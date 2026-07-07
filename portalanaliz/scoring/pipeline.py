"""Scoring pipeline: one LLM call per post.

Output per scored post is a binary signal — meaning depends on the prompt set
(uv4 = undervaluation, sent1 = positive sentiment) — stored in the
neutral post_scores.flagged column
plus the tickers it applies to (defaulting to the topic's own stock, since the
call returns only 0/1).

There is a single LLM stage: each post is sent to the model in its own call and
it answers with a single digit (0 or 1). The old two-stage "relevance filter ->
extract" design is gone; a chit-chat post just scores 0. Every post goes to the
LLM — the old length/keyword prefilter is gone too, and posts are sent in full
(no truncation).

Resumable by construction: a post is "done" when a post_scores row exists for
the active config (prompt set + model); each post commits its own row, so a
killed run loses at most the in-flight posts (one per worker).
--limit caps LLM-scored posts per run.
Switching prompt or model = a new config = a fresh scoring pass; old rows
stay for comparison. FOCUS_TICKERS restricts work to those topics.

Config identity in the DB is still (prompt_version, filter_model, extract_model);
with the filter stage removed, new rows store filter_model="" and
extract_model=<model>, which keeps them distinct from the old two-stage rows.
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
from portalanaliz.scoring.llm import LLMClient, LLMError, make_client, parse_score
from portalanaliz.scoring.textutil import strip_bbcode

log = logging.getLogger(__name__)

# Generous headroom: reasoning models on the /v1 path (qwen3+, deepseek-r1)
# spend completion tokens thinking before the single-digit answer.
MAX_TOKENS = 2000

_STAT_KEYS = ("scored", "flagged", "error",
              "input_tokens", "output_tokens", "cost_usd")

# Post + its topic + BBCode-stripped text, ready for the LLM.
_Prepared = tuple[Post, "Topic | None", str]


def _new_stats() -> dict:
    return {k: (0.0 if k == "cost_usd" else 0) for k in _STAT_KEYS}


def unscored_posts(session: Session, settings: ScoringSettings,
                   batch: int = 500) -> list[Post]:
    """Posts with no post_scores row for the active config, honoring FOCUS_TICKERS."""
    scored = select(PostScore.post_id).where(
        PostScore.prompt_version == settings.prompt,
        PostScore.filter_model == "",
        PostScore.extract_model == settings.model,
    )
    q = select(Post).where(Post.id.not_in(scored))
    if settings.tickers:
        topic_ids = select(Topic.id).where(Topic.ticker_hint.in_(settings.tickers))
        q = q.where(Post.topic_id.in_(topic_ids))
    # Newest posts first: the momentum/dashboard views only surface the last
    # few years, so scoring recent posts first makes their signal colors show
    # up right away instead of after the whole backlog is scored.
    # (SQLite sorts NULL post_time last under DESC, so dated posts lead.)
    return list(session.scalars(q.order_by(Post.post_time.desc()).limit(batch)))


def score_posts(session: Session, settings: ScoringSettings,
                limit: int | None = None, workers: int = 1) -> dict:
    """Process unscored posts. Returns counters incl. token/cost totals.

    `workers` > 1 runs the per-post LLM calls concurrently (each worker uses
    its own DB session and its own client). The resumability invariant (one
    committed row per post) is unchanged."""
    prompt_set = prompts.get_prompts(settings.prompt)
    stats = _new_stats()
    if workers > 1:
        _score_parallel(session, settings, prompt_set, stats, limit, workers)
    else:
        _score_sequential(session, settings, prompt_set, stats, limit)
    return stats


def _prepare(posts: list[Post], topics: dict[str, Topic]) -> list[_Prepared]:
    """Attach topic + stripped text to each post; everything goes to the LLM."""
    return [(post, topics.get(post.topic_id), strip_bbcode(post.content))
            for post in posts]


def _topics_for(session: Session, posts: list[Post]) -> dict[str, Topic]:
    return {t.id: t for t in session.scalars(
        select(Topic).where(Topic.id.in_({p.topic_id for p in posts})))}


def _score_sequential(session: Session, settings: ScoringSettings,
                      prompt_set: prompts.PromptSet, stats: dict,
                      limit: int | None) -> None:
    client: LLMClient | None = None
    llm_posts = 0  # posts that consumed LLM budget

    try:
        while True:
            posts = unscored_posts(session, settings)
            if not posts:
                break
            for item in _prepare(posts, _topics_for(session, posts)):
                if limit is not None and llm_posts >= limit:
                    log.info("--limit %d reached", limit)
                    return
                if client is None:
                    client = make_client(settings.model, settings)
                llm_posts += 1
                _score_post(session, settings, prompt_set, client, item, stats)
    finally:
        if client is not None:
            client.close()


def _score_parallel(session: Session, settings: ScoringSettings,
                    prompt_set: prompts.PromptSet, stats: dict,
                    limit: int | None, workers: int) -> None:
    """Fan the posts out to a thread pool. Each DB batch is drained before
    the next is fetched, so `unscored_posts` never re-hands a post already in
    flight."""
    tl = threading.local()

    def worker(item: _Prepared) -> dict:
        if not hasattr(tl, "client"):
            tl.client = make_client(settings.model, settings)
        delta = _new_stats()
        wsession = SessionLocal()
        try:
            _score_post(wsession, settings, prompt_set, tl.client, item, delta)
        finally:
            wsession.close()
        return delta

    llm_posts = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        while True:
            posts = unscored_posts(session, settings)
            if not posts:
                break
            prepared = _prepare(posts, _topics_for(session, posts))
            if limit is not None:
                prepared = prepared[:max(0, limit - llm_posts)]
            llm_posts += len(prepared)
            futures = [pool.submit(worker, item) for item in prepared]
            for fut in as_completed(futures):
                delta = fut.result()
                for k in _STAT_KEYS:
                    stats[k] += delta[k]
            # End the read transaction so the next unscored_posts() sees the
            # workers' commits (SQLite keeps a stale snapshot otherwise).
            session.rollback()
            if limit is not None and llm_posts >= limit:
                log.info("--limit %d reached", limit)
                break


def _score_post(session: Session, settings: ScoringSettings,
                prompt_set: prompts.PromptSet, client: LLMClient,
                item: _Prepared, stats: dict) -> None:
    """One LLM call for one post; write one row."""
    post, topic, text = item
    row = _new_row(settings, post, "error")
    try:
        user = prompts.post_user(topic.title if topic else "",
                                 topic.ticker_hint if topic else None,
                                 str(post.post_time or ""), text)
        r = client.complete(prompt_set.system, user, max_tokens=MAX_TOKENS)
        # Record usage before parsing: a parse failure still bills the call.
        row.input_tokens = r.input_tokens
        row.output_tokens = r.output_tokens
        row.cost_usd = r.cost_usd
        _add_usage_totals(stats, r)
        flag = parse_score(r.text)
        row.status = "scored"
        row.flagged = flag
        # The call returns only 0/1, so a flag lands on the topic's own stock
        # (the rollup's usual fallback).
        tickers = ([topic.ticker_hint]
                   if flag and topic and topic.ticker_hint else [])
        row.tickers_json = json.dumps(tickers, ensure_ascii=False)
        stats["scored"] += 1
        if flag:
            stats["flagged"] += 1
    except (LLMError, json.JSONDecodeError, ValueError) as exc:
        row.error = str(exc)[:1000]
        stats["error"] += 1
        log.warning("post %s failed: %s", post.id, exc)
    session.add(row)
    session.commit()
    log.info("post %s -> %s (flagged=%s, run total $%.4f)",
             post.id, row.status, bool(row.flagged), stats["cost_usd"])


def _new_row(settings: ScoringSettings, post: Post, status: str) -> PostScore:
    # filter_model="" marks the filterless pipeline; extract_model carries the
    # single scoring model. Together with prompt_version they form the config.
    return PostScore(post_id=post.id, prompt_version=settings.prompt, status=status,
                     filter_model="", extract_model=settings.model,
                     input_tokens=0, output_tokens=0, cost_usd=0.0)


def _add_usage_totals(stats: dict, r) -> None:
    stats["input_tokens"] += r.input_tokens
    stats["output_tokens"] += r.output_tokens
    stats["cost_usd"] += r.cost_usd
