"""SQLAlchemy models for the forum archive."""

from __future__ import annotations

import json
import zlib
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from portalanaliz.core.util import utcnow


class Base(DeclarativeBase):
    pass


class Forum(Base):
    __tablename__ = "forums"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # tapatalk forum_id
    name: Mapped[str] = mapped_column(String)
    parent_id: Mapped[str | None] = mapped_column(String, ForeignKey("forums.id"), nullable=True)
    sub_only: Mapped[bool] = mapped_column(Boolean, default=False)  # category, no topics
    # Whether the sync jobs should archive this forum's topics.
    is_indexed: Mapped[bool] = mapped_column(Boolean, default=True)
    last_topic_sync_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    topics: Mapped[list["Topic"]] = relationship(back_populates="forum")


class Topic(Base):
    __tablename__ = "topics"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # tapatalk topic_id
    forum_id: Mapped[str] = mapped_column(String, ForeignKey("forums.id"), index=True)
    title: Mapped[str] = mapped_column(String)
    # e.g. "SNT" extracted from "(SNT) SYNEKTIK"; filled by a later mapping pass.
    ticker_hint: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    reply_number: Mapped[int] = mapped_column(Integer, default=0)  # from topic listing
    total_post_num: Mapped[int | None] = mapped_column(Integer, nullable=True)  # from get_thread
    # Sync cursor: how many posts (positions 0..n-1) we have already stored.
    posts_fetched: Mapped[int] = mapped_column(Integer, default=0)
    is_sticky: Mapped[bool] = mapped_column(Boolean, default=False)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    forum: Mapped[Forum] = relationship(back_populates="topics")
    posts: Mapped[list["Post"]] = relationship(back_populates="topic")


class Author(Base):
    __tablename__ = "authors"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # tapatalk user_id
    username: Mapped[str] = mapped_column(String, index=True)


class Post(Base):
    __tablename__ = "posts"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # tapatalk post_id
    topic_id: Mapped[str] = mapped_column(String, ForeignKey("topics.id"), index=True)
    author_id: Mapped[str | None] = mapped_column(String, ForeignKey("authors.id"), index=True)
    author_name: Mapped[str | None] = mapped_column(String, nullable=True)
    position: Mapped[int] = mapped_column(Integer)  # 0-based index within topic
    post_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    title: Mapped[str | None] = mapped_column(String, nullable=True)
    content: Mapped[str] = mapped_column(Text)  # BBCode as served by the API
    # Full raw API payload for this post, zlib-compressed JSON (reparse insurance).
    raw_json: Mapped[bytes] = mapped_column(LargeBinary)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    topic: Mapped[Topic] = relationship(back_populates="posts")

    @property
    def attachments(self) -> list[dict]:
        """Attachment dicts from the raw payload (Tapatalk hosts images here)."""
        if not self.raw_json:
            return []
        return json.loads(zlib.decompress(self.raw_json)).get("attachments") or []


class PostScore(Base):
    """LLM scoring result for one post under one scoring config.

    A config is (prompt_version, filter_model, extract_model): a post is
    scored at most once per config, so switching prompt set or model starts a
    fresh, comparable experiment while old rows stay untouched. Rollup and the
    web UI only look at the currently configured combination.
    """

    __tablename__ = "post_scores"
    __table_args__ = (
        UniqueConstraint("post_id", "prompt_version", "filter_model", "extract_model"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    post_id: Mapped[str] = mapped_column(String, ForeignKey("posts.id"), index=True)
    prompt_version: Mapped[str] = mapped_column(String, index=True)
    # scored | error (skipped_short/skipped_keywords/chit_chat are legacy)
    status: Mapped[str] = mapped_column(String, index=True)
    is_analysis: Mapped[bool] = mapped_column(Boolean, default=False)
    # The binary signal (None = not scored). Meaning depends on the prompt set
    # (uv4 = undervaluation, sent1 = positive sentiment); column name is neutral.
    flagged: Mapped[bool | None] = mapped_column(Boolean, nullable=True, index=True)
    # JSON list of ticker strings the flag applies to.
    tickers_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)  # short reason (PL)
    # Legacy columns from the quality-scoring era (prompt_version "v1") — kept
    # so old rows stay readable; new prompt sets don't write them.
    quality: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    direction: Mapped[str | None] = mapped_column(String, nullable=True)
    claims_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Recorded on every row (even free skips) — part of the config identity.
    filter_model: Mapped[str] = mapped_column(String, default="")
    extract_model: Mapped[str] = mapped_column(String, default="")
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    scored_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    post: Mapped[Post] = relationship()


class StockScore(Base):
    """Per-ticker rollup: how many scored posts the active config flagged.

    One row per (ticker, as_of, config) where config = (prompt_version,
    filter_model, extract_model) — same identity as post_scores, so rollups of
    different prompts/models coexist instead of clobbering each other. as_of
    anchors to the newest scored post at rollup time — history accumulates as
    the archive/scoring grows.
    """

    __tablename__ = "stock_scores"
    __table_args__ = (
        UniqueConstraint("ticker", "as_of", "prompt_version",
                         "filter_model", "extract_model"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String, index=True)
    as_of: Mapped[date] = mapped_column(Date, index=True)
    # Config identity of the rollup (mirrors post_scores).
    prompt_version: Mapped[str] = mapped_column(String, index=True, default="")
    filter_model: Mapped[str] = mapped_column(String, default="")
    extract_model: Mapped[str] = mapped_column(String, default="")
    # Scored (LLM-judged) posts in this ticker's topics.
    posts_analyzed: Mapped[int] = mapped_column(Integer, default=0)
    # Posts the config flagged (signal=1) for this ticker.
    flagged_posts: Mapped[int] = mapped_column(Integer, default=0)
    computed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SyncState(Base):
    """Generic key/value store for sync bookkeeping."""

    __tablename__ = "sync_state"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
