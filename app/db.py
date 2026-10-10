"""SQLite engine, sessions, and initialization.

Foreign keys are enforced on every SQLite connection. Tests build isolated
engines with :func:`make_engine` pointing at temporary database files.
"""

from __future__ import annotations

from sqlalchemy import Engine, event, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app import config
from app.models import Base


def make_engine(url: str | None = None) -> Engine:
    db_url = url or config.DATABASE_URL
    kwargs: dict = {}
    if db_url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    engine = create_engine(db_url, **kwargs)
    if db_url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _enforce_foreign_keys(dbapi_connection, _connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def init_db(engine: Engine) -> None:
    Base.metadata.create_all(engine)


def make_session(engine: Engine) -> Session:
    return sessionmaker(bind=engine)()


def get_db():
    """FastAPI dependency yielding a session bound to the default engine."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


# Default application engine/session factory (used by the API in Phase 3).
engine = make_engine()
SessionLocal = sessionmaker(bind=engine)
