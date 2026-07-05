"""Per-ticker rollup of scored posts into stock_scores history rows.

attention = sum over recent analysis posts of (quality/100) * recency_decay
sentiment = attention-weighted mean of direction (+1 bull / -1 bear / 0 else)

One row per (ticker, as_of date); rerunning the same day overwrites, so the
dashboard chart gets one point per rollup day.
"""

from __future__ import annotations

import json
import logging
import math
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from portalanaliz.core.config import ScoringSettings
from portalanaliz.core.models import Post, PostScore, StockScore

log = logging.getLogger(__name__)

WINDOW_DAYS = 365
HALF_LIFE_DAYS = 90

_DIR_VALUE = {"bullish": 1.0, "bearish": -1.0}


def compute_stock_scores(session: Session, settings: ScoringSettings,
                         as_of: date | None = None) -> int:
    """Rollup over the ACTIVE config's scored rows only (stock_scores rows are
    overwritten per (ticker, date) — the table reflects the last config rolled up)."""
    scoped = (PostScore.prompt_version == settings.prompt,
              PostScore.filter_model == settings.filter_model,
              PostScore.extract_model == settings.extract_model,
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
    cutoff = datetime.combine(as_of, datetime.min.time()) - timedelta(days=WINDOW_DAYS)

    rows = session.execute(
        select(PostScore, Post.post_time)
        .join(Post, Post.id == PostScore.post_id)
        .where(*scoped, Post.post_time >= cutoff)
    ).all()

    # ticker -> [(weight, direction_value, quality)]
    per_ticker: dict[str, list[tuple[float, float, int]]] = {}
    for score, post_time in rows:
        if score.quality is None or not score.tickers_json:
            continue
        age_days = max(0.0, (datetime.combine(as_of, datetime.min.time())
                             - (post_time or cutoff)).total_seconds() / 86400)
        decay = math.exp(-math.log(2) * age_days / HALF_LIFE_DAYS)
        weight = (score.quality / 100.0) * decay
        for t in json.loads(score.tickers_json):
            ticker = t.get("ticker")
            if not ticker:
                continue
            direction = _DIR_VALUE.get(t.get("direction"), 0.0)
            per_ticker.setdefault(ticker, []).append((weight, direction, score.quality))

    for ticker, entries in per_ticker.items():
        total_w = sum(w for w, _, _ in entries)
        sentiment = (sum(w * d for w, d, _ in entries) / total_w) if total_w else 0.0
        existing = session.scalars(
            select(StockScore).where(StockScore.ticker == ticker, StockScore.as_of == as_of)
        ).first()
        row = existing or StockScore(ticker=ticker, as_of=as_of)
        row.attention = round(total_w, 4)
        row.sentiment = round(sentiment, 4)
        row.post_count = len(entries)
        row.avg_quality = round(sum(q for _, _, q in entries) / len(entries), 1)
        session.add(row)
    session.commit()
    log.info("stock rollup: %d tickers as of %s", len(per_ticker), as_of)
    return len(per_ticker)
