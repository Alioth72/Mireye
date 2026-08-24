# The Monitor - console (frontend)

A FastAPI server that does two things: serves the static console from
`static/`, and proxies every `/api/*` call through to the backend.

No build step. No bundler. The browser talks only to this origin, so there is
no CORS configuration and the backend's address never ships in the JavaScript.

## Run it

Two servers, two terminals. Backend first.

### 1. Backend (port 8000)

```powershell
cd C:\Projects\Mireye\backend
.venv\Scripts\python.exe -m uvicorn phase3.app:app --port 8000
```

### 2. Frontend (port 8080)

One-time setup:

```powershell
cd C:\Projects\Mireye\frontend
py -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env
```

Then edit `.env` - at minimum set `GOOGLE_MAPS_BROWSER_KEY` if you want a real
map. Leaving it empty is supported: the console falls back to an SVG vicinity
diagram rather than breaking.

Start the server:

```powershell
cd C:\Projects\Mireye\frontend
.venv\Scripts\python.exe -m uvicorn app:app --port 8080
```

Open <http://127.0.0.1:8080>.

`python app.py` also works and honours `FRONTEND_HOST` / `FRONTEND_PORT` from
`.env`; the explicit `uvicorn` line above is what the instructions above use so
the port is visible at the call site.

## Configuration

Everything lives in `.env` (gitignored) or the process environment. See
`.env.example` for the full annotated list.

The Google Maps browser key is **never** written into any file under `static/`.
It is read from the environment on each request and handed to the page by
`GET /api/config`. That key is public by nature - it is visible in the
browser's network tab - so it must be restricted by HTTP referrer in the Google
Cloud console. Details in `.env.example`.

## Endpoint map

The browser calls the left column; this server forwards to the right column on
`BACKEND_BASE_URL`. Query strings and JSON bodies pass through unchanged, and
so do upstream status codes: a backend 404 arrives at the browser as a 404, not
as an empty 200.

| Browser | Backend |
| --- | --- |
| `GET /api/config` | *(served locally - maps key, map ids, backend base url)* |
| `GET /api/health` | `GET /v1/healthz` *(never fails - see below)* |
| `GET /api/events` | `GET /phase1/events` |
| `GET /api/events/{id}` | `GET /phase1/events/{id}` |
| `GET /api/sites` | `GET /v1/sites` |
| `POST /api/sites` | `POST /v1/sites` |
| `GET /api/sites/{id}` | `GET /v1/sites/{id}` |
| `GET /api/sites/{id}/bundle/{name}` | `GET /v1/sites/{id}/{name}` |
| `GET /api/sites/{id}/vicinity` | `GET /v1/sites/{id}/vicinity` |
| `GET /api/sites/{id}/derived/{metric}` | `GET /v1/sites/{id}/derived/{metric}` |
| `GET /api/decisions` | `GET /v1/decisions` |
| `POST /api/decide` | `POST /v1/decide` |
| `GET /api/fetch-log` | `GET /v1/fetch-log` |
| `GET /api/budget` | `GET /v1/budget` |
| `GET /api/replay/runs` | `GET /v1/replay/runs` |

`vicinity` and `derived/{metric}` do not exist on the backend yet. They are
wired anyway and return a clean pass-through 404 until they do.

`/api/health` is the one deliberate exception to error pass-through: it always
returns 200, with `{"ok": false, "error": "..."}` when the backend is
unreachable, so a dead backend paints a red dot in the header instead of
crashing the console at boot. Every other route returns 502 when the backend
cannot be reached.

## Tests

```powershell
cd C:\Projects\Mireye\frontend
.venv\Scripts\python.exe -m pytest tests -q
```

They use `httpx.MockTransport`, so no backend and no network are needed.
