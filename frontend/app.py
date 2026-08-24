"""The Monitor console - frontend web server.

Two jobs, and deliberately nothing else:

1. Serve the static console (``frontend/static``) - no build step, no bundler.
2. Proxy every ``/api/*`` call through to the backend.

The proxy exists so the browser only ever talks to this origin. That removes
the CORS story entirely, keeps the backend address out of the shipped
JavaScript, and - most importantly - keeps the Google Maps browser key out of
the committed bundle: it is injected at request time by ``GET /api/config``
from the environment, never written into a file under ``static/``.

Run:  python -m uvicorn app:app --port 8080
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import quote

import httpx
from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from config import Settings, get_settings

STATIC_DIR = Path(__file__).resolve().parent / "static"
INDEX_HTML = STATIC_DIR / "index.html"

# Hop-by-hop / body-framing headers must not be copied from the upstream
# response: httpx has already decoded the body, so a stale content-encoding or
# content-length would describe bytes that no longer exist.
_DROP_RESPONSE_HEADERS = {
    "content-encoding",
    "content-length",
    "transfer-encoding",
    "connection",
    "keep-alive",
    "server",
    "date",
}


# ---------------------------------------------------------------------------
# lifespan: one shared AsyncClient for the whole process
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Open the shared upstream client on startup, close it on shutdown.

    A single pooled ``httpx.AsyncClient`` - creating one per request would
    throw away connection reuse and leak sockets under load.

    If a client has already been installed on ``app.state.http`` (the tests do
    this, with an ``httpx.MockTransport``) we adopt it and leave its lifetime
    to whoever put it there.
    """
    settings = get_settings()
    injected = getattr(app.state, "http", None)
    owns_client = injected is None

    if owns_client:
        app.state.http = httpx.AsyncClient(
            base_url=settings.backend_base_url.rstrip("/"),
            timeout=settings.proxy_timeout_s,
            follow_redirects=True,
        )
    try:
        yield
    finally:
        if owns_client:
            client: httpx.AsyncClient = app.state.http
            app.state.http = None
            await client.aclose()


# ---------------------------------------------------------------------------
# the proxy primitive
# ---------------------------------------------------------------------------
def _seg(value: str) -> str:
    """Re-encode one path segment for the upstream URL.

    Starlette hands us the *decoded* path parameter; a site id containing a
    slash or a space would otherwise re-split the upstream path.
    """
    return quote(str(value), safe="")


async def _forward(request: Request, backend_path: str) -> Response:
    """Forward the current request to ``backend_path`` on the backend.

    Query string and request body go up unchanged; the upstream status code
    and body come back down unchanged. 4xx and 5xx are NOT swallowed - the
    console needs to see a 404 as a 404 (several backend routes, vicinity and
    derived among them, do not exist yet, and a clean pass-through 404 is the
    correct answer for those).
    """
    client: httpx.AsyncClient | None = getattr(request.app.state, "http", None)
    if client is None:  # pragma: no cover - only reachable outside lifespan
        return JSONResponse({"detail": "proxy client is not running"}, status_code=503)

    body = await request.body()

    headers: dict[str, str] = {}
    ctype = request.headers.get("content-type")
    if ctype:
        headers["content-type"] = ctype
    accept = request.headers.get("accept")
    if accept:
        headers["accept"] = accept

    try:
        upstream = await client.request(
            request.method,
            backend_path,
            params=str(request.url.query) or None,
            content=body or None,
            headers=headers,
        )
    except httpx.RequestError as exc:
        # The backend is down or unreachable. 502 is honest; turning this into
        # a 200 would let the console render "no results" for "no server".
        return JSONResponse(
            {
                "detail": {
                    "error": "backend_unreachable",
                    "message": f"Cannot reach the backend at {backend_path}.",
                    "cause": str(exc),
                }
            },
            status_code=502,
        )

    passthrough = {
        k: v
        for k, v in upstream.headers.items()
        if k.lower() not in _DROP_RESPONSE_HEADERS
    }
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=passthrough,
        media_type=upstream.headers.get("content-type"),
    )


# ---------------------------------------------------------------------------
# app
# ---------------------------------------------------------------------------
def create_app() -> FastAPI:
    app = FastAPI(
        title="The Monitor - console",
        description="Static console plus a thin proxy onto the Monitor backend.",
        lifespan=lifespan,
    )

    # -- shell ---------------------------------------------------------------
    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(INDEX_HTML)

    # -- console configuration -----------------------------------------------
    @app.get("/api/config")
    async def api_config(settings: Settings = Depends(get_settings)) -> dict[str, Any]:
        """Runtime configuration for the browser.

        The Maps browser key is read from the environment (or the uncommitted
        ``.env``) on every request - it is never present in any file under
        ``static/``, so it cannot be committed by accident. When it is unset
        we return ``null`` rather than an empty string: the console has an SVG
        vicinity fallback and must degrade, not break.
        """
        return {
            "maps_key": settings.google_maps_browser_key or None,
            "map_id_light": settings.google_maps_map_id_light or None,
            "map_id_dark": settings.google_maps_map_id_dark or None,
            "backend_base_url": settings.backend_base_url,
        }

    # -- health ---------------------------------------------------------------
    @app.get("/api/health")
    async def api_health(request: Request) -> JSONResponse:
        """Backend liveness for the header dot.

        The one endpoint that deliberately never fails: a dead backend must
        paint a red dot, not blow up the console at boot. Always 200; the
        payload carries the truth.
        """
        client: httpx.AsyncClient | None = getattr(request.app.state, "http", None)
        if client is None:  # pragma: no cover
            return JSONResponse({"ok": False, "error": "proxy client is not running"})

        try:
            upstream = await client.get("/v1/healthz")
        except httpx.RequestError as exc:
            return JSONResponse({"ok": False, "error": str(exc) or exc.__class__.__name__})

        if upstream.status_code >= 400:
            return JSONResponse(
                {"ok": False, "error": f"backend returned {upstream.status_code}"}
            )

        try:
            payload = upstream.json()
        except ValueError:
            return JSONResponse({"ok": False, "error": "backend health was not JSON"})

        if isinstance(payload, dict):
            payload.setdefault("ok", True)
            return JSONResponse(payload)
        return JSONResponse({"ok": True, "backend": payload})

    # -- Phase 1: the public record -------------------------------------------
    @app.get("/api/events")
    async def api_events(request: Request) -> Response:
        return await _forward(request, "/phase1/events")

    @app.get("/api/events/{event_id:path}")
    async def api_event(request: Request, event_id: str) -> Response:
        return await _forward(request, f"/phase1/events/{_seg(event_id)}")

    # -- Phase 2: sites and physical facts ------------------------------------
    @app.get("/api/sites")
    async def api_sites(request: Request) -> Response:
        return await _forward(request, "/v1/sites")

    @app.post("/api/sites")
    async def api_create_site(request: Request) -> Response:
        return await _forward(request, "/v1/sites")

    @app.get("/api/sites/{site_id}")
    async def api_site(request: Request, site_id: str) -> Response:
        return await _forward(request, f"/v1/sites/{_seg(site_id)}")

    @app.get("/api/sites/{site_id}/vicinity")
    async def api_site_vicinity(request: Request, site_id: str) -> Response:
        # Not implemented backend-side yet; the 404 passes straight through.
        return await _forward(request, f"/v1/sites/{_seg(site_id)}/vicinity")

    @app.get("/api/sites/{site_id}/derived/{metric}")
    async def api_site_derived(request: Request, site_id: str, metric: str) -> Response:
        # Likewise not implemented yet - pass-through 404 is the right answer.
        return await _forward(
            request, f"/v1/sites/{_seg(site_id)}/derived/{_seg(metric)}"
        )

    @app.get("/api/sites/{site_id}/bundle/{name}")
    async def api_site_bundle(request: Request, site_id: str, name: str) -> Response:
        # Note the shape change: the browser says .../bundle/<name>, the
        # backend hangs bundles directly off the site: /v1/sites/<id>/<name>.
        return await _forward(request, f"/v1/sites/{_seg(site_id)}/{_seg(name)}")

    # -- Phase 3: decisions ---------------------------------------------------
    @app.get("/api/decisions")
    async def api_decisions(request: Request) -> Response:
        return await _forward(request, "/v1/decisions")

    @app.post("/api/decide")
    async def api_decide(request: Request) -> Response:
        return await _forward(request, "/v1/decide")

    # -- evidence / scorecard -------------------------------------------------
    @app.get("/api/fetch-log")
    async def api_fetch_log(request: Request) -> Response:
        return await _forward(request, "/v1/fetch-log")

    @app.get("/api/budget")
    async def api_budget(request: Request) -> Response:
        return await _forward(request, "/v1/budget")

    @app.get("/api/replay/runs")
    async def api_replay_runs(request: Request) -> Response:
        return await _forward(request, "/v1/replay/runs")

    # -- static bundle (mounted last so it cannot shadow /api/*) --------------
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    _s = get_settings()
    uvicorn.run("app:app", host=_s.frontend_host, port=_s.frontend_port, reload=False)
