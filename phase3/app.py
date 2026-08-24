"""The combined system: Phase 1 events, Phase 2 physical datapoints, Phase 3 decisions,
in one FastAPI process.

Phase 1's `monitor_records.api.app` is its own standalone `FastAPI()` (not an
`APIRouter` like Phase 2/3) -- rather than refactor Phase 1's already-tested module to
match Phase 2's router-first convention, it is mounted as a sub-application. Phase 2 and
Phase 3 both hang off a single `APIRouter` exactly as designed, so
`app.include_router(...)` is the whole integration for them.

    uvicorn phase3.app:app --reload --port 8000

    GET  /health                       Phase 1 (mounted)
    GET  /events                       Phase 1 (mounted)
    POST /v1/sites                     Phase 2
    GET  /v1/sites/{id}/{bundle}       Phase 2
    GET  /v1/fetch-log                 Phase 2
    POST /v1/decide                    Phase 3
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from monitor_records.api import app as phase1_app
from monitor_records.db import init_db as init_p1_db

from phase2.db import init_db as init_p2_db
from phase2.router import router as phase2_router

from phase3.db import init_db as init_p3_db
from phase3.router import router as phase3_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_p1_db()
    init_p2_db()
    init_p3_db()
    yield


app = FastAPI(
    title="The Monitor",
    description=(
        "Public government record -> structured event (Phase 1) -> Mireye physical "
        "features (Phase 2) -> materiality decision, ALERT or SILENCE (Phase 3)."
    ),
    version="0.1.0",
    lifespan=lifespan,
)
app.include_router(phase2_router)
app.include_router(phase3_router)
app.mount("/phase1", phase1_app)
