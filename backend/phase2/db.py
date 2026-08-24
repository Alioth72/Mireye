"""Database engine and session handling.

Deliberately free of any module-level FastAPI app so this drops into Phase 3's process
at merge time. Lifespan work lives in `init_db`, which Phase 3 can call from its own
startup.
"""

from __future__ import annotations

from typing import Iterator, Optional

from sqlmodel import Session, SQLModel, create_engine

from .config import get_settings

_engine = None


def get_engine():
    global _engine
    if _engine is None:
        settings = get_settings()
        connect_args = (
            {"check_same_thread": False} if settings.phase2_database_url.startswith("sqlite") else {}
        )
        _engine = create_engine(settings.phase2_database_url, connect_args=connect_args)
    return _engine


def init_db(engine=None) -> None:
    """Create Phase 2's tables. Safe to call repeatedly.

    All table names are prefixed `p2_`, so calling this against a database shared with
    Phase 3 cannot collide with theirs.
    """
    from . import models  # noqa: F401 -- registers tables on SQLModel.metadata

    SQLModel.metadata.create_all(engine or get_engine())


def get_session(engine=None) -> Iterator[Session]:
    """FastAPI dependency."""
    with Session(engine or get_engine()) as session:
        yield session


def reset_engine(engine=None) -> None:
    """Test hook: swap or clear the process-wide engine."""
    global _engine
    _engine = engine
