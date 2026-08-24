"""Phase 3's engine/session handling. Mirrors phase2/db.py's pattern (SQLModel, a
module-global engine, a `reset_engine` test hook) rather than Phase 1's SQLAlchemy-
declarative style, since pipeline.py already needs a SQLModel Session in scope to call
into phase2.store/phase2.scoring."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator, Optional

from dotenv import load_dotenv
from sqlmodel import Session, SQLModel, create_engine

_BACKEND_DIR = Path(__file__).resolve().parents[1]
load_dotenv(_BACKEND_DIR.parent / ".env")
load_dotenv(_BACKEND_DIR / ".env", override=True)

_DATABASE_URL = os.environ.get("PHASE3_DATABASE_URL", "sqlite:///./phase3.db")
_engine = None


def get_engine():
    global _engine
    if _engine is None:
        connect_args = {"check_same_thread": False} if _DATABASE_URL.startswith("sqlite") else {}
        _engine = create_engine(_DATABASE_URL, connect_args=connect_args)
    return _engine


def init_db(engine=None) -> None:
    # NOTE: SQLModel tables all register onto one shared SQLModel.metadata per process,
    # so if phase2.models has already been imported (pipeline.py imports phase2.store),
    # create_all here will also create phase2's p2_* tables in phase3.db (empty, unused)
    # -- and phase2's own init_db() will likewise create an empty p3_decision table in
    # phase2.db. Harmless (idempotent DDL, no cross-writes -- each engine is only ever
    # queried through its own session) but worth knowing before you go looking for why
    # a table exists somewhere you didn't expect it.
    from . import models  # noqa: F401 -- registers P3Decision on SQLModel.metadata

    SQLModel.metadata.create_all(engine or get_engine())


def get_session(engine=None) -> Iterator[Session]:
    with Session(engine or get_engine()) as session:
        yield session


def reset_engine(engine=None) -> None:
    global _engine
    _engine = engine
