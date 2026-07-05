"""Scoring pipeline: prefilter -> LLM relevance filter -> LLM extraction.

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

from sqlalchemy import select
from sqlalchemy.orm import Session

from portalanaliz.core.config import ScoringSettings
from portalanaliz.core.models import Post, PostScore, Topic
from portalanaliz.scoring import prompts
from portalanaliz.scoring.llm import LLMClient, LLMError, make_client, parse_json_response
from portalanaliz.scoring.textutil import MIN_CHARS, has_analysis_signal, strip_bbcode

log = logging.getLogger(__name__)

VALID_DIRECTIONS = {"bullish", "bearish", "neutral", "mixed"}


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
                limit: int | None = None) -> dict:
    """Process unscored posts. Returns counters incl. token/cost totals."""
    prompt_set = prompts.get_prompts(settings.prompt)
    stats = {"skipped_short": 0, "skipped_keywords": 0, "chit_chat": 0,
             "scored": 0, "error": 0, "input_tokens": 0, "output_tokens": 0,
             "cost_usd": 0.0}
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
                    return stats
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
    return stats


def _score_one(session: Session, settings: ScoringSettings, prompt_set: prompts.PromptSet,
               filter_client: LLMClient, extract_client: LLMClient,
               post: Post, topic: Topic | None, text: str, stats: dict) -> None:
    title = topic.title if topic else ""
    row = _new_row(settings, post, "error")
    try:
        r = filter_client.complete(prompt_set.filter_system,
                                   prompts.filter_user(title, text), max_tokens=64)
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
                max_tokens=1500,
            )
            _add_usage(row, stats, r)
            data = parse_json_response(r.text)
            _apply_extraction(row, data, topic)
            row.status = "scored"
            stats["scored"] += 1
    except (LLMError, json.JSONDecodeError, ValueError) as exc:
        row.error = str(exc)[:1000]
        stats["error"] += 1
        log.warning("post %s: %s", post.id, exc)
    session.add(row)
    session.commit()
    log.info("post %s -> %s (q=%s, cost=$%.5f, run total $%.4f)",
             post.id, row.status, row.quality, row.cost_usd, stats["cost_usd"])


def _apply_extraction(row: PostScore, data: dict, topic: Topic | None) -> None:
    tickers = [t for t in (data.get("tickers") or []) if isinstance(t, dict)]
    for t in tickers:
        if t.get("direction") not in VALID_DIRECTIONS:
            t["direction"] = "neutral"
        if t.get("ticker"):
            t["ticker"] = str(t["ticker"]).upper()
    row.tickers_json = json.dumps(tickers, ensure_ascii=False)
    claims = [str(c) for c in (data.get("claims") or [])][:8]
    row.claims_json = json.dumps(claims, ensure_ascii=False)
    row.summary = str(data.get("summary") or "")[:1000] or None
    quality = data.get("quality")
    row.quality = max(0, min(100, int(quality))) if quality is not None else None

    # Post-level direction = the primary ticker's (fallback: topic ticker, then first).
    primary = next((t for t in tickers if t.get("is_primary")), None)
    if primary is None and topic and topic.ticker_hint:
        primary = next((t for t in tickers if t.get("ticker") == topic.ticker_hint), None)
    if primary is None and tickers:
        primary = tickers[0]
    row.direction = primary.get("direction") if primary else None


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
