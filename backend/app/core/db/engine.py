"""Engine and session lifecycle.

QGL_DATABASE_URL selects the backend. Unset means "no relational store
configured" and every caller must degrade to the SQLite path rather than
assuming a database exists — is_configured() is how they ask.

The URL is normalised to the psycopg3 driver, because "postgresql://..." picked
up psycopg2 by default in SQLAlchemy and produced a confusing ModuleNotFoundError
on a URL that looks correct.
"""
from __future__ import annotations

import os
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Base

_engine: Engine | None = None
_Session: sessionmaker | None = None


def _normalise(url: str) -> str:
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    if url.startswith("postgres://"):            # the form some hosts hand out
        return url.replace("postgres://", "postgresql+psycopg://", 1)
    return url


DATABASE_URL = _normalise(os.getenv("QGL_DATABASE_URL", "")) or None


def is_configured() -> bool:
    return bool(DATABASE_URL)


def get_engine(url: str | None = None, echo: bool = False) -> Engine:
    """Process-wide engine. Passing a url rebuilds it, which is how the tests
    point at a throwaway database without touching the environment."""
    global _engine, _Session
    target = _normalise(url) if url else DATABASE_URL
    if not target:
        raise RuntimeError(
            "No relational store configured. Set QGL_DATABASE_URL, e.g. "
            "postgresql://user:pass@localhost:5432/quantgenlabs"
        )
    if _engine is None or (url and str(_engine.url) != target):
        dispose()
        kw: dict = {"echo": echo, "future": True}
        if target.startswith("sqlite"):
            # SQLite needs this to be usable from FastAPI's threadpool at all.
            kw["connect_args"] = {"check_same_thread": False}
        else:
            # pool_pre_ping costs one round trip and avoids handing out a
            # connection the server closed while it sat idle -- the classic
            # "server closed the connection unexpectedly" after a quiet period.
            kw.update(pool_pre_ping=True, pool_size=5, max_overflow=10, pool_recycle=1800)
        _engine = create_engine(target, **kw)
        _Session = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def create_all(url: str | None = None) -> None:
    """Create the schema directly. Convenient for tests and a first local run;
    Alembic owns schema change for anything that has data in it."""
    Base.metadata.create_all(get_engine(url))


@contextmanager
def session_scope(url: str | None = None) -> Session:
    """Transactional scope. Commits on success, rolls back on any exception —
    so a failed multi-statement write leaves no half-applied state."""
    get_engine(url)
    assert _Session is not None
    s = _Session()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def dispose() -> None:
    global _engine, _Session
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _Session = None
