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


def init_db() -> None:
    from portalanaliz.core import models  # noqa: F401  (register tables)

    DATA_DIR.mkdir(exist_ok=True)
    MEDIA_DIR.mkdir(exist_ok=True)
    models.Base.metadata.create_all(engine)


def get_session() -> Session:
    return SessionLocal()
