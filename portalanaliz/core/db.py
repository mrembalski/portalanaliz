"""Database engine/session setup. SQLite lives in data/."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
DB_PATH = DATA_DIR / "portalanaliz.db"

# timeout: tolerate concurrent writers (e.g. two scoring configs running
# side by side) instead of failing fast with "database is locked".
engine = create_engine(f"sqlite:///{DB_PATH}", future=True,
                       connect_args={"timeout": 30})
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


def _post_scores_is_legacy(conn) -> bool:
    """True if post_scores exists with the old (post_id, prompt_version) unique key."""
    exists = conn.exec_driver_sql(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='post_scores'"
    ).first()
    if not exists:
        return False
    for _, name, unique, *_ in conn.exec_driver_sql("PRAGMA index_list('post_scores')"):
        if not unique:
            continue
        cols = [r[2] for r in conn.exec_driver_sql(f"PRAGMA index_info('{name}')")]
        if cols == ["post_id", "prompt_version"]:
            return True
    return False


_POST_SCORES_COLS = (
    "id, post_id, prompt_version, status, is_analysis, quality, direction, "
    "tickers_json, claims_json, summary, error, filter_model, extract_model, "
    "input_tokens, output_tokens, cost_usd, scored_at"
)


def init_db() -> None:
    from portalanaliz.core import models  # noqa: F401  (register tables)

    DATA_DIR.mkdir(exist_ok=True)

    # Migration: widen post_scores' unique key to include the model pair
    # (rename old -> create_all makes fresh table -> copy -> drop). Each step
    # is keyed on observable DB state, so an interrupted migration resumes.
    with engine.begin() as conn:
        if _post_scores_is_legacy(conn):
            conn.exec_driver_sql("ALTER TABLE post_scores RENAME TO post_scores_legacy")
            # Named indexes ride along with the rename and would collide with
            # the fresh table's CREATE INDEX — drop them from the legacy copy.
            named = conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND tbl_name='post_scores_legacy' AND name NOT LIKE 'sqlite_%'"
            ).all()
            for (idx_name,) in named:
                conn.exec_driver_sql(f'DROP INDEX "{idx_name}"')

    # Additive/derived-table migrations for the undervalued-signal pivot.
    with engine.begin() as conn:
        cols = {r[1] for r in conn.exec_driver_sql("PRAGMA table_info('post_scores')")}
        if cols and "undervalued" not in cols:
            conn.exec_driver_sql("ALTER TABLE post_scores ADD COLUMN undervalued BOOLEAN")
            # create_all skips existing tables, so add the index here.
            conn.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_post_scores_undervalued "
                "ON post_scores (undervalued)")
        # stock_scores content is derived from post_scores — safe to rebuild
        # when the schema changed (old shape had attention/sentiment columns).
        s_cols = {r[1] for r in conn.exec_driver_sql("PRAGMA table_info('stock_scores')")}
        if s_cols and "undervalued_posts" not in s_cols:
            conn.exec_driver_sql("DROP TABLE stock_scores")

    models.Base.metadata.create_all(engine)

    with engine.begin() as conn:
        legacy = conn.exec_driver_sql(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='post_scores_legacy'"
        ).first()
        if legacy:
            select_cols = _POST_SCORES_COLS.replace(
                "filter_model, extract_model",
                "coalesce(filter_model, ''), coalesce(extract_model, '')",
            )
            conn.exec_driver_sql(
                f"INSERT OR IGNORE INTO post_scores ({_POST_SCORES_COLS}) "
                f"SELECT {select_cols} FROM post_scores_legacy"
            )
            conn.exec_driver_sql("DROP TABLE post_scores_legacy")

    with engine.begin() as conn:
        for stmt in _FTS_STATEMENTS:
            conn.exec_driver_sql(stmt)
        posts = conn.exec_driver_sql("SELECT count(*) FROM posts").scalar() or 0
        indexed = conn.exec_driver_sql("SELECT count(*) FROM posts_fts").scalar() or 0
        if posts != indexed:
            conn.exec_driver_sql("INSERT INTO posts_fts(posts_fts) VALUES('rebuild')")


def get_session() -> Session:
    return SessionLocal()
