"""Per-ticker rollup: count flagged signals into stock_scores rows.

For each ticker:
- posts_analyzed — scored (LLM-judged) posts in that ticker's topics
- flagged_posts  — posts whose signal (1) names the ticker

One row per (ticker, as_of, config) where config = the active
(prompt, filter, extract) triple and as_of anchors to the newest scored post at
rollup time; rerunning the same config at the same anchor overwrites, and new
archive data moves the anchor forward, building history the dashboard can chart.
"""

from __future__ import annotations

import json
import logging
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from portalanaliz.core.config import ScoringSettings
from portalanaliz.core.models import Post, PostScore, StockScore, Topic

log = logging.getLogger(__name__)


def compute_stock_scores(session: Session, settings: ScoringSettings,
                         as_of: date | None = None) -> int:
    """Rollup over the ACTIVE config's scored rows into stock_scores rows keyed
    by (ticker, date, config), so each config keeps its own rollup history."""
    scoped = (PostScore.prompt_version == settings.prompt,
              PostScore.filter_model == "",
              PostScore.extract_model == settings.model,
              PostScore.status == "scored")
    if as_of is None:
        # Anchor to the newest scored post, not today — during backfill the
        # archive tail is years old and a today-anchored window would be empty.
        newest = session.execute(
            select(func.max(Post.post_time))
            .join(PostScore, PostScore.post_id == Post.id)
            .where(*scoped)
        ).scalar()
        as_of = newest.date() if newest else date.today()

    # Denominator: scored posts per topic ticker.
    analyzed = dict(session.execute(
        select(Topic.ticker_hint, func.count(PostScore.id))
        .join(Post, Post.topic_id == Topic.id)
        .join(PostScore, PostScore.post_id == Post.id)
        .where(*scoped, Topic.ticker_hint.is_not(None))
        .group_by(Topic.ticker_hint)
    ).all())

    # Numerator: flagged signals per ticker named in tickers_json.
    flagged: dict[str, int] = {}
    rows = session.scalars(
        select(PostScore.tickers_json).where(*scoped, PostScore.flagged.is_(True))
    ).all()
    for tickers_json in rows:
        for ticker in json.loads(tickers_json or "[]"):
            flagged[ticker] = flagged.get(ticker, 0) + 1

    for ticker in set(analyzed) | set(flagged):
        existing = session.scalars(
            select(StockScore).where(
                StockScore.ticker == ticker, StockScore.as_of == as_of,
                StockScore.prompt_version == settings.prompt,
                StockScore.filter_model == "",
                StockScore.extract_model == settings.model)
        ).first()
        row = existing or StockScore(
            ticker=ticker, as_of=as_of, prompt_version=settings.prompt,
            filter_model="", extract_model=settings.model)
        row.posts_analyzed = analyzed.get(ticker, 0)
        row.flagged_posts = flagged.get(ticker, 0)
        session.add(row)
    session.commit()
    n = len(set(analyzed) | set(flagged))
    log.info("stock rollup: %d tickers as of %s", n, as_of)
    return n
