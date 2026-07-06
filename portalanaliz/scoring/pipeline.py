"""Scoring pipeline: prefilter -> LLM relevance filter -> LLM undervaluation call.

Output per scored post is a binary undervaluation signal (post_scores.undervalued)
plus the tickers it applies to and a one-line reason.

Resumable by construction: a post is "done" when a post_scores row exists for
the active config (prompt set + filter model + extract model); each post
commits individually, so a killed run loses at most one in-flight post.
--limit caps LLM-scored posts per run (free prefilter skips don't count).
Switching prompt or model = a new config = a fresh scoring pass; old rows
stay for comparison. FOCUS_TICKERS restricts work to those topics.
"""

from __future__ import annotations

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from sqlalchemy import select
from sqlalchemy.orm import Session

from portalanaliz.core.config import ScoringSettings
from portalanaliz.core.db import SessionLocal
from portalanaliz.core.models import Post, PostScore, Topic
from portalanaliz.scoring import prompts
from portalanaliz.scoring.llm import LLMClient, LLMError, make_client, parse_json_response
from portalanaliz.scoring.textutil import MIN_CHARS, has_analysis_signal, strip_bbcode

log = logging.getLogger(__name__)

_STAT_KEYS = ("skipped_short", "skipped_keywords", "chit_chat", "scored",
              "undervalued", "error", "input_tokens", "output_tokens", "cost_usd")


def unscored_posts(session: Session, settings: ScoringSettings,
                   batch: int = 500) -> list[Post]:
    """Posts with no post_scores row for the active config, honoring FOCUS_TICKERS."""
    scored = select(PostScore.post_id).where(
        PostScore.prompt_version == settings.prompt,
        PostScore.filter_model == settings.filter_model,
        PostScore.extract_model == settings.extract_model,
    )
    q = select(Post).where(Post.id.not_in(scored))
    if settings.tickers:
        topic_ids = select(Topic.id).where(Topic.ticker_hint.in_(settings.tickers))
        q = q.where(Post.topic_id.in_(topic_ids))
    return list(session.scalars(q.order_by(Post.post_time).limit(batch)))


def score_posts(session: Session, settings: ScoringSettings,
                limit: int | None = None, workers: int = 1) -> dict:
    """Process unscored posts. Returns counters incl. token/cost totals.

    `workers` > 1 runs the LLM stage concurrently across posts (each worker
    uses its own DB session and its own clients). The free prefilter and the
    resumability invariant (one committed row per post) are unchanged."""
    prompt_set = prompts.get_prompts(settings.prompt)
    stats = {k: (0.0 if k == "cost_usd" else 0) for k in _STAT_KEYS}
    if workers > 1:
        _score_parallel(session, settings, prompt_set, stats, limit, workers)
    else:
        _score_sequential(session, settings, prompt_set, stats, limit)
    return stats


def _score_sequential(session: Session, settings: ScoringSettings,
                      prompt_set: prompts.PromptSet, stats: dict,
                      limit: int | None) -> None:
    filter_client: LLMClient | None = None
    extract_client: LLMClient | None = None
    llm_calls_for = 0  # posts that consumed LLM budget

    try:
        while True:
            posts = unscored_posts(session, settings)
            if not posts:
                break
            topics = {t.id: t for t in session.scalars(
                select(Topic).where(Topic.id.in_({p.topic_id for p in posts})))}
            for post in posts:
                if limit is not None and llm_calls_for >= limit:
                    log.info("--limit %d reached", limit)
                    return
                text = strip_bbcode(post.content)
                if len(text) < MIN_CHARS:
                    _save(session, settings, post, "skipped_short")
                    stats["skipped_short"] += 1
                    continue
                if not has_analysis_signal(text):
                    _save(session, settings, post, "skipped_keywords")
                    stats["skipped_keywords"] += 1
                    continue

                if filter_client is None:
                    filter_client = make_client(settings.filter_model, settings)
                    extract_client = make_client(settings.extract_model, settings)
                llm_calls_for += 1
                topic = topics.get(post.topic_id)
                _score_one(session, settings, prompt_set, filter_client,
                           extract_client, post, topic, text, stats)
    finally:
        for c in (filter_client, extract_client):
            if c is not None:
                c.close()


def _score_parallel(session: Session, settings: ScoringSettings,
                    prompt_set: prompts.PromptSet, stats: dict,
                    limit: int | None, workers: int) -> None:
    """Prefilter in the main thread (free skips committed immediately); fan the
    LLM-bound posts out to a thread pool. Each batch is drained before the next
    is fetched, so `unscored_posts` never re-hands a post already in flight."""
    tl = threading.local()

    def worker(post: Post, topic: Topic | None, text: str) -> dict:
        if not hasattr(tl, "clients"):
            fc = make_client(settings.filter_model, settings)
            ec = (fc if settings.extract_model == settings.filter_model
                  else make_client(settings.extract_model, settings))
            tl.clients = (fc, ec)
        fc, ec = tl.clients
        delta = {k: (0.0 if k == "cost_usd" else 0) for k in _STAT_KEYS}
        wsession = SessionLocal()
        try:
            _score_one(wsession, settings, prompt_set, fc, ec, post, topic, text, delta)
        finally:
            wsession.close()
        return delta

    llm_calls_for = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        while True:
            posts = unscored_posts(session, settings)
            if not posts:
                break
            topics = {t.id: t for t in session.scalars(
                select(Topic).where(Topic.id.in_({p.topic_id for p in posts})))}
            futures = []
            for post in posts:
                text = strip_bbcode(post.content)
                if len(text) < MIN_CHARS:
                    _save(session, settings, post, "skipped_short")
                    stats["skipped_short"] += 1
                    continue
                if not has_analysis_signal(text):
                    _save(session, settings, post, "skipped_keywords")
                    stats["skipped_keywords"] += 1
                    continue
                if limit is not None and llm_calls_for >= limit:
                    break
                llm_calls_for += 1
                futures.append(pool.submit(worker, post, topics.get(post.topic_id), text))
            for fut in as_completed(futures):
                delta = fut.result()
                for k in _STAT_KEYS:
                    stats[k] += delta[k]
            if limit is not None and llm_calls_for >= limit:
                log.info("--limit %d reached", limit)
                break


def _score_one(session: Session, settings: ScoringSettings, prompt_set: prompts.PromptSet,
               filter_client: LLMClient, extract_client: LLMClient,
               post: Post, topic: Topic | None, text: str, stats: dict) -> None:
    title = topic.title if topic else ""
    row = _new_row(settings, post, "error")
    try:
        # Generous max_tokens: reasoning models (qwen3+, deepseek-r1) spend
        # completion tokens on thinking before the tiny JSON answer — a tight
        # cap truncates mid-reasoning and yields empty content.
        r = filter_client.complete(prompt_set.filter_system,
                                   prompts.filter_user(title, text), max_tokens=2000)
        _add_usage(row, stats, r)
        is_analysis = bool(parse_json_response(r.text).get("analysis"))
        row.is_analysis = is_analysis
        if not is_analysis:
            row.status = "chit_chat"
            stats["chit_chat"] += 1
        else:
            r = extract_client.complete(
                prompt_set.extract_system,
                prompts.extraction_user(title, topic.ticker_hint if topic else None,
                                        str(post.post_time or ""), text),
                max_tokens=3000,
            )
            _add_usage(row, stats, r)
            data = parse_json_response(r.text)
            _apply_extraction(row, data, topic)
            row.status = "scored"
            stats["scored"] += 1
            if row.undervalued:
                stats["undervalued"] += 1
    except (LLMError, json.JSONDecodeError, ValueError) as exc:
        row.error = str(exc)[:1000]
        stats["error"] += 1
        log.warning("post %s: %s", post.id, exc)
    session.add(row)
    session.commit()
    log.info("post %s -> %s (undervalued=%s, cost=$%.5f, run total $%.4f)",
             post.id, row.status, row.undervalued, row.cost_usd, stats["cost_usd"])


def _apply_extraction(row: PostScore, data: dict, topic: Topic | None) -> None:
    row.undervalued = bool(data.get("undervalued"))
    tickers = [str(t).upper() for t in (data.get("tickers") or []) if t]
    # The signal must land on some ticker; default to the topic's own stock.
    if row.undervalued and not tickers and topic and topic.ticker_hint:
        tickers = [topic.ticker_hint]
    row.tickers_json = json.dumps(tickers if row.undervalued else [],
                                  ensure_ascii=False)
    row.summary = str(data.get("reason") or "")[:1000] or None


def _new_row(settings: ScoringSettings, post: Post, status: str) -> PostScore:
    return PostScore(post_id=post.id, prompt_version=settings.prompt, status=status,
                     filter_model=settings.filter_model,
                     extract_model=settings.extract_model,
                     input_tokens=0, output_tokens=0, cost_usd=0.0)


def _save(session: Session, settings: ScoringSettings, post: Post, status: str) -> None:
    session.add(_new_row(settings, post, status))
    session.commit()


def _add_usage(row: PostScore, stats: dict, r) -> None:
    row.input_tokens += r.input_tokens
    row.output_tokens += r.output_tokens
    row.cost_usd += r.cost_usd
    stats["input_tokens"] += r.input_tokens
    stats["output_tokens"] += r.output_tokens
    stats["cost_usd"] += r.cost_usd
