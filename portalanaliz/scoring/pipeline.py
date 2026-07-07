"""Scoring pipeline: one batched LLM undervaluation call per BATCH_SIZE posts.

Output per scored post is a binary undervaluation signal (post_scores.undervalued)
plus the tickers it applies to (defaulting to the topic's own stock, since the
batched call returns only 0/1 per post).

There is a single LLM stage: posts are sent to the model in BATCHES (up to
BATCH_SIZE at a time) and it returns one 0/1 per post. The old two-stage
"relevance filter -> extract" design is gone; a chit-chat post just scores 0.
Every post goes to the LLM — the old length/keyword prefilter is gone too.

Resumable by construction: a post is "done" when a post_scores row exists for
the active config (prompt set + model); each batch commits its rows together,
so a killed run loses at most one in-flight batch (BATCH_SIZE posts).
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
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed

from sqlalchemy import select
from sqlalchemy.orm import Session

from portalanaliz.core.config import ScoringSettings
from portalanaliz.core.db import SessionLocal
from portalanaliz.core.models import Post, PostScore, Topic
from portalanaliz.scoring import prompts
from portalanaliz.scoring.llm import LLMClient, LLMError, make_client, parse_scores
from portalanaliz.scoring.textutil import strip_bbcode

log = logging.getLogger(__name__)

# Posts per LLM call. The model gets this many numbered posts and returns one
# 0/1 per post.
BATCH_SIZE = 5

_STAT_KEYS = ("scored", "undervalued", "error",
              "input_tokens", "output_tokens", "cost_usd")

# Post + its topic + BBCode-stripped text, ready for the LLM.
_Prepared = tuple[Post, "Topic | None", str]


def _new_stats() -> dict:
    return {k: (0.0 if k == "cost_usd" else 0) for k in _STAT_KEYS}


def _batch_max_tokens(n: int) -> int:
    """Generous headroom: reasoning models (qwen3+, deepseek-r1) spend
    completion tokens thinking about each post before the tiny JSON answer;
    scale with batch size so nothing truncates mid-reasoning."""
    return 1500 + 600 * n


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
    # few years, so scoring recent posts first makes their undervaluation
    # colors show up right away instead of after the whole backlog is scored.
    # (SQLite sorts NULL post_time last under DESC, so dated posts lead.)
    return list(session.scalars(q.order_by(Post.post_time.desc()).limit(batch)))


def score_posts(session: Session, settings: ScoringSettings,
                limit: int | None = None, workers: int = 1) -> dict:
    """Process unscored posts. Returns counters incl. token/cost totals.

    `workers` > 1 runs the batched LLM calls concurrently (each worker uses its
    own DB session and its own client). The resumability invariant (one
    committed batch of rows) is unchanged."""
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


def _chunks(seq: list, n: int) -> Iterator[list]:
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


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
            prepared = _prepare(posts, _topics_for(session, posts))
            for group in _chunks(prepared, BATCH_SIZE):
                if limit is not None:
                    remaining = limit - llm_posts
                    if remaining <= 0:
                        log.info("--limit %d reached", limit)
                        return
                    group = group[:remaining]
                if client is None:
                    client = make_client(settings.model, settings)
                llm_posts += len(group)
                _score_batch(session, settings, prompt_set, client, group, stats)
    finally:
        if client is not None:
            client.close()


def _score_parallel(session: Session, settings: ScoringSettings,
                    prompt_set: prompts.PromptSet, stats: dict,
                    limit: int | None, workers: int) -> None:
    """Fan the batches out to a thread pool. Each DB batch is drained before
    the next is fetched, so `unscored_posts` never re-hands a post already in
    flight."""
    tl = threading.local()

    def worker(group: list[_Prepared]) -> dict:
        if not hasattr(tl, "client"):
            tl.client = make_client(settings.model, settings)
        delta = _new_stats()
        wsession = SessionLocal()
        try:
            _score_batch(wsession, settings, prompt_set, tl.client, group, delta)
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
            futures = []
            for group in _chunks(prepared, BATCH_SIZE):
                if limit is not None:
                    remaining = limit - llm_posts
                    if remaining <= 0:
                        break
                    group = group[:remaining]
                llm_posts += len(group)
                futures.append(pool.submit(worker, group))
            for fut in as_completed(futures):
                delta = fut.result()
                for k in _STAT_KEYS:
                    stats[k] += delta[k]
            if limit is not None and llm_posts >= limit:
                log.info("--limit %d reached", limit)
                break


def _score_batch(session: Session, settings: ScoringSettings,
                 prompt_set: prompts.PromptSet, client: LLMClient,
                 group: list[_Prepared], stats: dict) -> None:
    """One LLM call for up to BATCH_SIZE posts; write one row per post."""
    rows = [_new_row(settings, post, "error") for post, _, _ in group]
    try:
        items = [(topic.title if topic else "",
                  topic.ticker_hint if topic else None,
                  str(post.post_time or ""), text)
                 for post, topic, text in group]
        r = client.complete(prompt_set.system, prompts.batch_user(items),
                            max_tokens=_batch_max_tokens(len(group)))
        # Record usage before parsing: a parse failure still bills the call.
        _split_usage(rows, r)
        _add_usage_totals(stats, r)
        flags = parse_scores(r.text, len(group))
        for (post, topic, _), row, flag in zip(group, rows, flags):
            row.status = "scored"
            row.undervalued = flag
            # The batch returns only 0/1, so an undervaluation signal lands on
            # the topic's own stock (the rollup's usual fallback).
            tickers = ([topic.ticker_hint]
                       if flag and topic and topic.ticker_hint else [])
            row.tickers_json = json.dumps(tickers, ensure_ascii=False)
            stats["scored"] += 1
            if flag:
                stats["undervalued"] += 1
    except (LLMError, json.JSONDecodeError, ValueError) as exc:
        msg = str(exc)[:1000]
        for row in rows:
            row.error = msg
        stats["error"] += len(rows)
        log.warning("batch of %d failed: %s", len(rows), exc)
    for row in rows:
        session.add(row)
    session.commit()
    undervalued = sum(1 for row in rows if row.undervalued)
    log.info("batch of %d -> scored (undervalued=%d, run total $%.4f)",
             len(rows), undervalued, stats["cost_usd"])


def _new_row(settings: ScoringSettings, post: Post, status: str) -> PostScore:
    # filter_model="" marks the filterless pipeline; extract_model carries the
    # single scoring model. Together with prompt_version they form the config.
    return PostScore(post_id=post.id, prompt_version=settings.prompt, status=status,
                     filter_model="", extract_model=settings.model,
                     input_tokens=0, output_tokens=0, cost_usd=0.0)


def _split_usage(rows: list[PostScore], r) -> None:
    """Spread one batch call's tokens/cost across its rows (remainder to the
    first) so per-post costs still roughly sum to the call's real usage."""
    n = len(rows) or 1
    in_each, in_rem = divmod(r.input_tokens, n)
    out_each, out_rem = divmod(r.output_tokens, n)
    cost_each = r.cost_usd / n
    for i, row in enumerate(rows):
        row.input_tokens = in_each + (in_rem if i == 0 else 0)
        row.output_tokens = out_each + (out_rem if i == 0 else 0)
        row.cost_usd = cost_each


def _add_usage_totals(stats: dict, r) -> None:
    stats["input_tokens"] += r.input_tokens
    stats["output_tokens"] += r.output_tokens
    stats["cost_usd"] += r.cost_usd
