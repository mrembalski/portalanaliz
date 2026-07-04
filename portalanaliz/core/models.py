"""SQLAlchemy models for the forum archive."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
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
    media: Mapped[list["Media"]] = relationship(back_populates="post")


class Media(Base):
    __tablename__ = "media"
    __table_args__ = (UniqueConstraint("post_id", "source_url"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    post_id: Mapped[str] = mapped_column(String, ForeignKey("posts.id"), index=True)
    source_url: Mapped[str] = mapped_column(String)
    kind: Mapped[str] = mapped_column(String, default="inline")  # inline | attachment
    status: Mapped[str] = mapped_column(String, default="pending", index=True)  # pending|done|failed
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    local_path: Mapped[str | None] = mapped_column(String, nullable=True)  # relative to data/media
    sha256: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    mime: Mapped[str | None] = mapped_column(String, nullable=True)
    size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    post: Mapped[Post] = relationship(back_populates="media")


class SyncState(Base):
    """Generic key/value store for sync bookkeeping."""

    __tablename__ = "sync_state"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
