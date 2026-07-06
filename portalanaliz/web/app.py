"""FastAPI app: browse and search the local forum archive."""

from __future__ import annotations

import json
import re
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy import case, func, select, text
from sqlalchemy.orm import Session

from portalanaliz.core.config import load_scoring_settings
from portalanaliz.core.db import SessionLocal, init_db
from portalanaliz.core.models import (
    Author, Forum, Post, PostScore, StockScore, Topic,
)
from portalanaliz.web.bbcode import render as render_bbcode

PAGE_SIZE = 50

init_db()
# The UI shows results for the active scoring config only (env at startup).
SCORING = load_scoring_settings()


def _scoped_scores():
    # Filterless pipeline: new rows store filter_model="" + extract_model=model.
    return (PostScore.prompt_version == SCORING.prompt,
            PostScore.filter_model == "",
            PostScore.extract_model == SCORING.model)

app = FastAPI(title="PortalAnaliz Archive")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.filters["fromjson"] = json.loads


def db() -> Session:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _render_posts(session: Session, posts: list[Post]) -> list[dict]:
    """Attach rendered HTML to posts."""
    return [{"post": p, "html": render_bbcode(p.content)} for p in posts]


@app.get("/")
def index(request: Request, config: str = "", session: Session = Depends(db)):
    cfg = _resolve_config(config)
    stats = {
        "posts": session.scalar(select(func.count(Post.id))) or 0,
        "topics": session.scalar(select(func.count(Topic.id))) or 0,
        "topics_started": session.scalar(
            select(func.count(Topic.id)).where(Topic.posts_fetched > 0)) or 0,
        "topics_complete": session.scalar(select(func.count(Topic.id)).where(
            Topic.posts_fetched >= func.coalesce(Topic.total_post_num, Topic.reply_number + 1))) or 0,
        "authors": session.scalar(select(func.count(Author.id))) or 0,
        "last_fetch": session.scalar(select(func.max(Post.fetched_at))),
    }
    top_topics = session.scalars(
        select(Topic).order_by(Topic.reply_number.desc()).limit(15)
    ).all()
    recent = session.scalars(
        select(Post).order_by(Post.fetched_at.desc()).limit(10)
    ).all()
    topic_titles = {
        t.id: t for t in session.scalars(
            select(Topic).where(Topic.id.in_({p.topic_id for p in recent}))).all()
    }
    scoring = {
        "undervalued": session.scalar(select(func.count(PostScore.id))
                                      .where(*_scoped_scores(),
                                             PostScore.undervalued.is_(True))) or 0,
        "scored": session.scalar(select(func.count(PostScore.id))
                                 .where(*_scoped_scores(),
                                        PostScore.status == "scored")) or 0,
        "processed": session.scalar(select(func.count(PostScore.id))
                                    .where(*_scoped_scores())) or 0,
        "cost": session.scalar(select(func.sum(PostScore.cost_usd))
                               .where(*_scoped_scores())) or 0.0,
    }
    top_stocks = _latest_stock_scores(session, limit=10)
    hot = [r for r in _momentum_rows(session, config=cfg) if r["trend"] > 0][:5]
    return templates.TemplateResponse(request, "index.html", {
        "stats": stats,
        "top_topics": top_topics, "recent": recent, "topic_titles": topic_titles,
        "scoring": scoring, "top_stocks": top_stocks, "hot": hot,
        "configs": _scoring_configs(session), "config_key": config,
    })


def _latest_stock_scores(session: Session, limit: int | None = None) -> list[StockScore]:
    """Most recent stock_scores row per ticker, most undervaluation signals first."""
    latest = (
        select(StockScore.ticker, func.max(StockScore.as_of).label("as_of"))
        .group_by(StockScore.ticker).subquery()
    )
    q = (
        select(StockScore)
        .join(latest, (StockScore.ticker == latest.c.ticker)
              & (StockScore.as_of == latest.c.as_of))
        .order_by(StockScore.undervalued_posts.desc(),
                  StockScore.posts_analyzed.desc())
    )
    if limit:
        q = q.limit(limit)
    return list(session.scalars(q))


def _uv_color(pct: float | None) -> str:
    """Undervalued share -> line color: gray (no data), slate 0% -> green 100%."""
    if pct is None:
        return "#d1d5db"
    lo, hi = (0x64, 0x74, 0x8B), (0x16, 0xA3, 0x4A)
    t = max(0.0, min(1.0, pct))
    return "#%02x%02x%02x" % tuple(round(a + (b - a) * t) for a, b in zip(lo, hi))


def _next_month(m: str) -> str:
    y, mo = int(m[:4]), int(m[5:7])
    return f"{y + mo // 12}-{(mo % 12) + 1:02d}"


def _scoring_configs(session: Session) -> list[dict]:
    """Every scoring config (prompt + models) that has scored rows, most rows
    first. Each carries a `key` ("prompt|filter|extract") for the UI selector."""
    rows = session.execute(
        select(PostScore.prompt_version, PostScore.filter_model,
               PostScore.extract_model, func.count(PostScore.id).label("n"))
        .where(PostScore.status == "scored")
        .group_by(PostScore.prompt_version, PostScore.filter_model,
                  PostScore.extract_model)
        .order_by(func.count(PostScore.id).desc())
    ).all()
    active = (SCORING.prompt, "", SCORING.model)
    out = []
    for pv, fm, em, n in rows:
        out.append({
            "key": f"{pv}|{fm}|{em}",
            "prompt": pv, "filter_model": fm, "extract_model": em,
            "label": f"{pv} · {em}", "scored": n,
            "is_active": (pv, fm, em) == active,
        })
    return out


def _resolve_config(key: str | None) -> tuple[str, str, str] | None:
    """Query-param config key -> (prompt, filter, extract) tuple, or None to
    let the momentum view fall back to the active/most-scored config."""
    if not key:
        return None
    parts = key.split("|")
    return (parts[0], parts[1], parts[2]) if len(parts) == 3 else None


def _momentum_rows(session: Session, window: int = 12, span: int = 36,
                   min_posts: int = 10,
                   config: tuple[str, str, str] | None = None) -> list[dict]:
    """Per ticker: derivative of the rolling {window}-month post count, as
    sparkline segments whose color encodes the share of that month's scored
    posts flagging undervaluation. Coloring uses `config` when given, else the
    active config, else the config with the most scored rows for the ticker."""
    counts = session.execute(
        select(Topic.ticker_hint, func.strftime("%Y-%m", Post.post_time).label("m"),
               func.count(Post.id))
        .join(Post, Post.topic_id == Topic.id)
        .where(Topic.ticker_hint.is_not(None), Post.post_time.is_not(None))
        .group_by(Topic.ticker_hint, "m")
    ).all()
    by_ticker: dict[str, dict[str, int]] = {}
    for ticker, month, n in counts:
        by_ticker.setdefault(ticker, {})[month] = n
    if not by_ticker:
        return []
    last_month = max(m for months in by_ticker.values() for m in months)

    scored = session.execute(
        select(Topic.ticker_hint, func.strftime("%Y-%m", Post.post_time),
               PostScore.prompt_version, PostScore.filter_model,
               PostScore.extract_model, PostScore.undervalued, PostScore.tickers_json)
        .join(Post, Post.id == PostScore.post_id)
        .join(Topic, Topic.id == Post.topic_id)
        .where(PostScore.status == "scored", Topic.ticker_hint.is_not(None))
    ).all()
    # ticker -> config -> month -> [scored, undervalued]
    uv_raw: dict[str, dict[tuple, dict[str, list[int]]]] = {}
    for ticker, month, pv, fm, em, uv, tj in scored:
        bucket = uv_raw.setdefault(ticker, {}).setdefault((pv, fm, em), {}) \
                       .setdefault(month, [0, 0])
        bucket[0] += 1
        if uv and ticker in (json.loads(tj) if tj else []):
            bucket[1] += 1
    preferred = config or (SCORING.prompt, "", SCORING.model)

    out = []
    # ~7px per derivative point: short histories draw a short line (left-padded,
    # so the most recent month stays flush to the right edge) instead of
    # stretching a few points across the full width. Capped at max_width.
    px_per_point, max_width, height = 7, 240, 44
    for ticker, months in by_ticker.items():
        if sum(months.values()) < min_posts:
            continue
        # contiguous month axis: first post -> newest month in the archive
        axis, m = [], min(months)
        while m <= last_month:
            axis.append(m)
            m = _next_month(m)
        series = [months.get(m, 0) for m in axis]
        rolling = [sum(series[max(0, i - window + 1):i + 1]) for i in range(len(series))]
        deriv = [rolling[i] - rolling[i - 1] for i in range(1, len(rolling))]
        d_axis = axis[1:]
        if len(deriv) < 2:
            continue
        deriv, d_axis = deriv[-span:], d_axis[-span:]

        configs = uv_raw.get(ticker, {})
        if config is not None:
            # Explicit pick: show only that config's judgment (no cross-config fallback).
            chosen = configs.get(config, {})
        else:
            chosen = configs.get(preferred) or (
                max(configs.values(), key=lambda c: sum(v[0] for v in c.values()))
                if configs else {})
        uv_pct = {m: (v[1] / v[0] if v[0] else None) for m, v in chosen.items()}

        lo, hi = min(deriv), max(deriv)
        spread = (hi - lo) or 1
        line_w = min(max_width, px_per_point * max(1, len(deriv) - 1))
        x0 = max_width - line_w  # left pad; recent month flush to the right
        step = line_w / max(1, len(deriv) - 1)
        pts = [(round(x0 + i * step, 1),
                round(height - 4 - (d - lo) / spread * (height - 8), 1))
               for i, d in enumerate(deriv)]
        segments = [
            (*pts[i], *pts[i + 1], _uv_color(uv_pct.get(d_axis[i + 1])))
            for i in range(len(pts) - 1)
        ]
        zero_y = round(height - 4 - (0 - lo) / spread * (height - 8), 1)
        recent_scored = sum(v[0] for m, v in chosen.items() if m in d_axis[-window:])
        recent_uv = sum(v[1] for m, v in chosen.items() if m in d_axis[-window:])
        out.append({
            "ticker": ticker,
            "segments": segments,
            "zero_y": zero_y if lo <= 0 <= hi else None,
            "trend": round(sum(deriv[-3:]) / min(3, len(deriv)), 1),
            "posts_window": rolling[-1],
            "uv_recent": (recent_uv / recent_scored) if recent_scored else None,
            "w": max_width, "x0": x0, "h": height,
        })
    out.sort(key=lambda r: r["trend"], reverse=True)
    return out


@app.get("/momentum")
def momentum(request: Request, config: str = "", session: Session = Depends(db)):
    cfg = _resolve_config(config)
    return templates.TemplateResponse(request, "momentum.html", {
        "rows": _momentum_rows(session, config=cfg)[:50],
        "configs": _scoring_configs(session), "config_key": config,
    })


@app.get("/stocks")
def stocks(request: Request, session: Session = Depends(db)):
    return templates.TemplateResponse(request, "stocks.html", {
        "rows": _latest_stock_scores(session),
    })


@app.get("/stocks/{ticker}")
def stock_view(ticker: str, request: Request, session: Session = Depends(db)):
    ticker = ticker.upper()
    history = session.scalars(
        select(StockScore).where(StockScore.ticker == ticker)
        .order_by(StockScore.as_of.desc()).limit(60)
    ).all()
    # Posts flagging this ticker as undervalued, newest first. Prefer the
    # active config; if it has nothing (e.g. only another model scored this
    # ticker), fall back to all configs deduped by post.
    def _flagged(scoped: bool):
        q = (select(PostScore, Post)
             .join(Post, Post.id == PostScore.post_id)
             .where(PostScore.undervalued.is_(True),
                    PostScore.tickers_json.like(f'%"{ticker}"%'))
             .order_by(Post.post_time.desc()).limit(200))
        if scoped:
            q = q.where(*_scoped_scores())
        rows, seen = [], set()
        for score, post in session.execute(q):
            if ticker not in json.loads(score.tickers_json or "[]"):
                continue  # LIKE false positive (ticker substring of another)
            if post.id in seen:
                continue
            seen.add(post.id)
            rows.append({"score": score, "post": post})
        return rows

    best = _flagged(scoped=True)
    other_config = False
    if not best:
        best = _flagged(scoped=False)
        other_config = bool(best)
    topic_map = {
        t.id: t for t in session.scalars(select(Topic).where(
            Topic.id.in_({b["post"].topic_id for b in best}))).all()
    } if best else {}
    topics_for_ticker = session.scalars(
        select(Topic).where(Topic.ticker_hint == ticker)).all()
    # Every config that judged posts in this ticker's topics — model comparison.
    flagged = case(
        (PostScore.undervalued.is_(True)
         & PostScore.tickers_json.like(f'%"{ticker}"%'), 1),
        else_=0)
    configs = session.execute(
        select(PostScore.prompt_version, PostScore.filter_model,
               PostScore.extract_model,
               func.count(PostScore.id).label("analyzed"),
               func.sum(flagged).label("undervalued"))
        .join(Post, Post.id == PostScore.post_id)
        .join(Topic, Topic.id == Post.topic_id)
        .where(Topic.ticker_hint == ticker, PostScore.status == "scored")
        .group_by(PostScore.prompt_version, PostScore.filter_model,
                  PostScore.extract_model)
        .order_by(func.count(PostScore.id).desc())
    ).all()
    active = (SCORING.prompt, "", SCORING.model)
    if not history and not best and not topics_for_ticker:
        raise HTTPException(404)
    return templates.TemplateResponse(request, "stock.html", {
        "ticker": ticker, "history": history, "best": best,
        "topic_map": topic_map, "topics_for_ticker": topics_for_ticker,
        "configs": configs, "active_config": active,
        "other_config": other_config,
    })


@app.get("/forums")
def forums(request: Request, session: Session = Depends(db)):
    all_forums = session.scalars(select(Forum)).all()
    topic_counts = dict(session.execute(
        select(Topic.forum_id, func.count(Topic.id)).group_by(Topic.forum_id)).all())
    archived_counts = dict(session.execute(
        select(Topic.forum_id, func.count(Topic.id))
        .where(Topic.posts_fetched > 0).group_by(Topic.forum_id)).all())

    by_parent: dict[str | None, list[Forum]] = {}
    for f in all_forums:
        by_parent.setdefault(f.parent_id, []).append(f)

    rows: list[tuple[Forum, int]] = []

    def walk(parent_id: str | None, depth: int) -> None:
        for f in by_parent.get(parent_id, []):
            rows.append((f, depth))
            walk(f.id, depth + 1)

    walk(None, 0)
    return templates.TemplateResponse(request, "forums.html", {
        "rows": rows, "topic_counts": topic_counts, "archived_counts": archived_counts,
    })


@app.get("/forums/{forum_id}")
def forum_view(forum_id: str, request: Request, page: int = Query(1, ge=1),
               session: Session = Depends(db)):
    forum = session.get(Forum, forum_id)
    if forum is None:
        raise HTTPException(404)
    total = session.scalar(select(func.count(Topic.id)).where(Topic.forum_id == forum_id)) or 0
    topics = session.scalars(
        select(Topic).where(Topic.forum_id == forum_id)
        .order_by(Topic.is_sticky.desc(), Topic.reply_number.desc())
        .offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE)
    ).all()
    return templates.TemplateResponse(request, "forum.html", {
        "forum": forum, "topics": topics, "page": page,
        "pages": max(1, -(-total // PAGE_SIZE)),
    })


@app.get("/topics/{topic_id}")
def topic_view(topic_id: str, request: Request, page: int = Query(1, ge=1),
               session: Session = Depends(db)):
    topic = session.get(Topic, topic_id)
    if topic is None:
        raise HTTPException(404)
    total = session.scalar(select(func.count(Post.id)).where(Post.topic_id == topic_id)) or 0
    posts = session.scalars(
        select(Post).where(Post.topic_id == topic_id).order_by(Post.position)
        .offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE)
    ).all()
    return templates.TemplateResponse(request, "topic.html", {
        "topic": topic, "items": _render_posts(session, posts), "page": page,
        "pages": max(1, -(-total // PAGE_SIZE)), "total": total,
    })


@app.get("/search")
def search(request: Request, q: str = "", page: int = Query(1, ge=1),
           session: Session = Depends(db)):
    results, total, error = [], 0, None
    if q.strip():
        # Quote each token so user input can't break FTS5 query syntax.
        tokens = re.findall(r"\S+", q)
        match = " ".join('"' + t.replace('"', "") + '"' for t in tokens)
        try:
            total = session.execute(
                text("SELECT count(*) FROM posts_fts WHERE posts_fts MATCH :m"),
                {"m": match},
            ).scalar() or 0
            rows = session.execute(text("""
                SELECT p.id, p.topic_id, p.author_name, p.post_time, p.position,
                       snippet(posts_fts, 0, '<mark>', '</mark>', ' … ', 40) AS snip
                FROM posts_fts JOIN posts p ON p.rowid = posts_fts.rowid
                WHERE posts_fts MATCH :m
                ORDER BY rank LIMIT :lim OFFSET :off
            """), {"m": match, "lim": PAGE_SIZE, "off": (page - 1) * PAGE_SIZE}).all()
            topic_map = {
                t.id: t for t in session.scalars(
                    select(Topic).where(Topic.id.in_({r.topic_id for r in rows}))).all()
            } if rows else {}
            results = [{"row": r, "topic": topic_map.get(r.topic_id)} for r in rows]
        except Exception as exc:  # malformed FTS query
            error = str(exc)
    return templates.TemplateResponse(request, "search.html", {
        "q": q, "results": results, "total": total, "error": error,
        "page": page, "pages": max(1, -(-total // PAGE_SIZE)),
    })


@app.get("/authors")
def authors(request: Request, session: Session = Depends(db)):
    rows = session.execute(
        select(Post.author_id, Post.author_name, func.count(Post.id).label("n"),
               func.max(Post.post_time).label("last"))
        .group_by(Post.author_id).order_by(func.count(Post.id).desc()).limit(200)
    ).all()
    return templates.TemplateResponse(request, "authors.html", {"rows": rows})


@app.get("/authors/{author_id}")
def author_view(author_id: str, request: Request, page: int = Query(1, ge=1),
                session: Session = Depends(db)):
    author = session.get(Author, author_id)
    if author is None:
        raise HTTPException(404)
    total = session.scalar(select(func.count(Post.id)).where(Post.author_id == author_id)) or 0
    posts = session.scalars(
        select(Post).where(Post.author_id == author_id).order_by(Post.post_time.desc())
        .offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE)
    ).all()
    topic_map = {
        t.id: t for t in session.scalars(
            select(Topic).where(Topic.id.in_({p.topic_id for p in posts}))).all()
    } if posts else {}
    return templates.TemplateResponse(request, "author.html", {
        "author": author, "items": _render_posts(session, posts), "topic_map": topic_map,
        "page": page, "pages": max(1, -(-total // PAGE_SIZE)), "total": total,
    })
