"""Standalone FastAPI app for solo development.

This is the ONLY file the merge into Phase 3 may discard. Everything else hangs off
``phase2.router``; Phase 3 does ``app.include_router(phase2.router)`` and calls
``init_db()`` from its own lifespan.

    uvicorn phase2.app:app --reload --port 8002
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from .db import init_db
from .router import router


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Phase 2 — Mireye Data Service",
    description=(
        "Provenance-tagged physical datapoints for a monitored coordinate. "
        "Knows nothing about events: Phase 3 combines these with public-record events "
        "and decides whether to alert."
    ),
    version="0.1.0",
    lifespan=lifespan,
)
app.include_router(router)
