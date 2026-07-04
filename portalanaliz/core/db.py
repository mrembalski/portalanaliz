"""Database engine/session setup. SQLite lives in data/."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
DB_PATH = DATA_DIR / "portalanaliz.db"
MEDIA_DIR = DATA_DIR / "media"

engine = create_engine(f"sqlite:///{DB_PATH}", future=True)
SessionLocal = sessionmaker(bind=engine, future=True, expire_on_commit=False)


# Full-text search over posts: external-content FTS5 table kept in sync
# by triggers; rebuilt automatically if counts drift (e.g. first creation
# over an already-populated posts table).
_FTS_STATEMENTS = [
    """CREATE VIRTUAL TABLE IF NOT EXISTS posts_fts USING fts5(
        content, author_name, title,
        content='posts', content_rowid='rowid'
    )""",
    """CREATE TRIGGER IF NOT EXISTS posts_fts_ai AFTER INSERT ON posts BEGIN
        INSERT INTO posts_fts(rowid, content, author_name, title)
        VALUES (new.rowid, new.content, new.author_name, new.title);
    END""",
    """CREATE TRIGGER IF NOT EXISTS posts_fts_ad AFTER DELETE ON posts BEGIN
        INSERT INTO posts_fts(posts_fts, rowid, content, author_name, title)
        VALUES ('delete', old.rowid, old.content, old.author_name, old.title);
    END""",
    """CREATE TRIGGER IF NOT EXISTS posts_fts_au AFTER UPDATE ON posts BEGIN
        INSERT INTO posts_fts(posts_fts, rowid, content, author_name, title)
        VALUES ('delete', old.rowid, old.content, old.author_name, old.title);
        INSERT INTO posts_fts(rowid, content, author_name, title)
        VALUES (new.rowid, new.content, new.author_name, new.title);
    END""",
]


def init_db() -> None:
    from portalanaliz.core import models  # noqa: F401  (register tables)

    DATA_DIR.mkdir(exist_ok=True)
    MEDIA_DIR.mkdir(exist_ok=True)
    models.Base.metadata.create_all(engine)

    with engine.begin() as conn:
        for stmt in _FTS_STATEMENTS:
            conn.exec_driver_sql(stmt)
        posts = conn.exec_driver_sql("SELECT count(*) FROM posts").scalar() or 0
        indexed = conn.exec_driver_sql("SELECT count(*) FROM posts_fts").scalar() or 0
        if posts != indexed:
            conn.exec_driver_sql("INSERT INTO posts_fts(posts_fts) VALUES('rebuild')")


def get_session() -> Session:
    return SessionLocal()
