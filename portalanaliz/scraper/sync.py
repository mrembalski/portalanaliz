"""Forum sync: forums tree, topic listings, posts (backfill + incremental).

All modes are resumable and budget-aware:
- Topic cursor (`Topic.posts_fetched`) means a killed run never re-downloads posts.
- `--budget N` caps outgoing API requests per run; the job exits cleanly when spent.

Usage:
    python -m portalanaliz.scraper.sync forums
    python -m portalanaliz.scraper.sync topics [--forum-id ID]
    python -m portalanaliz.scraper.sync posts  [--forum-id ID] [--budget N]
    python -m portalanaliz.scraper.sync all    [--budget N]
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import zlib
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from portalanaliz.core.config import load_settings
from portalanaliz.core.db import get_session, init_db
from portalanaliz.core.models import Author, Forum, Post, Topic
from portalanaliz.core.util import utcnow
from portalanaliz.scraper.tapatalk import RequestBudgetExceeded, TapatalkClient

log = logging.getLogger(__name__)

TOPICS_PER_PAGE = 50
POSTS_PER_PAGE = 50

# "(SNT) SYNEKTIK" or "[LTM] LONGTERM GAMES" -> "SNT" / "LTM"
TICKER_RE = re.compile(r"^[(\[]([A-Z0-9]{2,6})[)\]]")


# ------------------------------------------------------------------- forums

def sync_forums(client: TapatalkClient, session: Session) -> None:
    tree = client.get_forum()
    count = _upsert_forums(session, tree, parent_id=None)
    session.commit()
    log.info("forums synced: %d", count)


def _upsert_forums(session: Session, forums: list, parent_id: str | None) -> int:
    count = 0
    for f in forums:
        fid = str(f["forum_id"])
        forum = session.get(Forum, fid)
        if forum is None:
            forum = Forum(id=fid, name=f.get("forum_name", ""), parent_id=parent_id)
            session.add(forum)
        forum.name = f.get("forum_name", forum.name)
        forum.parent_id = parent_id
        forum.sub_only = bool(f.get("sub_only", False))
        count += 1 + _upsert_forums(session, f.get("child") or [], parent_id=fid)
    return count


# ------------------------------------------------------------------- topics

def sync_topics(client: TapatalkClient, session: Session, forum_id: str | None = None) -> None:
    if forum_id:
        forums = [session.get(Forum, forum_id)]
    else:
        forums = session.scalars(
            select(Forum).where(Forum.sub_only.is_(False), Forum.is_indexed.is_(True))
        ).all()

    for forum in forums:
        if forum is None:
            continue
        for mode, sticky in (("", False), ("TOP", True)):
            _sync_forum_topics(client, session, forum, mode=mode, sticky=sticky)
        forum.last_topic_sync_at = utcnow()
        session.commit()


def _sync_forum_topics(client: TapatalkClient, session: Session, forum: Forum,
                       mode: str, sticky: bool) -> None:
    start = 0
    total = None
    while total is None or start < total:
        page = client.get_topics(forum.id, start, start + TOPICS_PER_PAGE - 1, mode=mode)
        topics = page.get("topics") or []
        total = int(page.get("total_topic_num") or 0)
        if not topics:
            break
        for t in topics:
            _upsert_topic(session, forum.id, t, sticky=sticky)
        session.commit()
        start += len(topics)
        log.info("forum %s (%s): %d/%d topics", forum.id, mode or "normal", min(start, total), total)


def _upsert_topic(session: Session, forum_id: str, t: dict, sticky: bool) -> None:
    tid = str(t["topic_id"])
    topic = session.get(Topic, tid)
    title = t.get("topic_title", "")
    if topic is None:
        topic = Topic(id=tid, forum_id=forum_id, title=title)
        session.add(topic)
    topic.title = title or topic.title
    topic.forum_id = forum_id
    topic.reply_number = int(t.get("reply_number") or 0)
    topic.is_sticky = sticky or topic.is_sticky
    m = TICKER_RE.match(title)
    if m:
        topic.ticker_hint = m.group(1)


# -------------------------------------------------------------------- posts

def sync_posts(client: TapatalkClient, session: Session, forum_id: str | None = None,
               tickers: tuple[str, ...] = (), max_posts: int | None = None) -> None:
    """Fetch missing posts for every topic whose cursor lags its post count.

    tickers (FOCUS_TICKERS) narrows the topic selection only — cursor and
    budget behavior are unchanged, so widening the list later just makes more
    topics eligible.

    max_posts (exclusive) restricts to smaller threads: only topics whose
    expected post count is < max_posts are fetched. Lets a run prioritise
    finishing the many small topics before the few giant ones.
    """
    q = select(Topic)
    if forum_id:
        q = q.where(Topic.forum_id == forum_id)
    if tickers:
        q = q.where(Topic.ticker_hint.in_(tickers))
        log.info("posts sync limited to tickers: %s", ", ".join(tickers))
    topics = session.scalars(q).all()

    # Expected post count: reply_number + 1 (first post isn't a "reply").
    def _expected(t: Topic) -> int:
        return t.total_post_num or t.reply_number + 1

    if max_posts is not None:
        topics = [t for t in topics if _expected(t) < max_posts]
        log.info("posts sync limited to topics with < %d posts", max_posts)
    todo = [t for t in topics if t.posts_fetched < _expected(t)]
    # Finish partially-fetched topics first, then oldest-synced, so budgeted
    # runs don't leave threads half-archived.
    todo.sort(key=lambda t: (t.posts_fetched == 0, t.last_synced_at or datetime.min, t.id))
    log.info("posts sync: %d topics need fetching", len(todo))

    for topic in todo:
        _sync_topic_posts(client, session, topic)


def _sync_topic_posts(client: TapatalkClient, session: Session, topic: Topic) -> None:
    while True:
        expected = topic.total_post_num or topic.reply_number + 1
        if topic.posts_fetched >= expected:
            break
        start = topic.posts_fetched
        page = client.get_thread(topic.id, start, start + POSTS_PER_PAGE - 1)
        posts = page.get("posts") or []
        topic.total_post_num = int(page.get("total_post_num") or expected)
        if not posts:
            # Count drift (deleted posts); accept reality and stop.
            topic.posts_fetched = topic.total_post_num
            break
        for i, p in enumerate(posts):
            _upsert_post(session, topic.id, position=start + i, payload=p)
        topic.posts_fetched = start + len(posts)
        topic.last_synced_at = utcnow()
        session.commit()
        log.info("topic %s (%s): %d/%d posts", topic.id, topic.title[:40],
                 topic.posts_fetched, topic.total_post_num)


def _upsert_post(session: Session, topic_id: str, position: int, payload: dict) -> None:
    pid = str(payload["post_id"])

    author_id = str(payload.get("post_author_id") or "") or None
    author_name = payload.get("post_author_name")
    if author_id and session.get(Author, author_id) is None:
        session.add(Author(id=author_id, username=author_name or ""))

    post = session.get(Post, pid)
    if post is None:
        post = Post(id=pid, topic_id=topic_id, position=position, content="", raw_json=b"")
        session.add(post)

    post.topic_id = topic_id
    post.position = position
    post.author_id = author_id
    post.author_name = author_name
    post.title = payload.get("post_title")
    post.content = payload.get("post_content") or ""
    post.post_time = _to_naive_utc(payload.get("post_time"))
    post.raw_json = zlib.compress(json.dumps(payload, default=str).encode())
    post.fetched_at = utcnow()


def _to_naive_utc(value: object) -> datetime | None:
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(timezone.utc).replace(tzinfo=None)
        return value
    return None


# ---------------------------------------------------------------------- CLI

def main() -> None:
    parser = argparse.ArgumentParser(description="Sync portalanaliz.pl forum data")
    parser.add_argument("command", choices=["forums", "topics", "posts", "all"])
    parser.add_argument("--forum-id", help="restrict to one forum")
    parser.add_argument("--budget", type=int, default=None,
                        help="max outgoing API requests this run")
    parser.add_argument("--max-posts", type=int, default=None,
                        help="only fetch topics with fewer than N posts "
                             "(prioritise small threads)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    init_db()
    settings = load_settings()
    session = get_session()

    with TapatalkClient(settings) as client:
        client.max_requests = args.budget
        try:
            if args.command in ("forums", "all"):
                sync_forums(client, session)
            if args.command in ("topics", "all"):
                sync_topics(client, session, forum_id=args.forum_id)
            if args.command in ("posts", "all"):
                sync_posts(client, session, forum_id=args.forum_id,
                           tickers=settings.focus_tickers,
                           max_posts=args.max_posts)
        except RequestBudgetExceeded as exc:
            session.commit()
            log.info("stopping: %s (progress saved, rerun to continue)", exc)

    log.info("API requests made this run: %d", client.requests_made)
    session.close()


if __name__ == "__main__":
    main()
