# The Monitor

Agentic monitoring system: public government record → structured event (Phase 1) →
Mireye physical features (Phase 2) → materiality decision, `ALERT`/`SILENCE` (Phase 3).
Conservative by design — a false alert is worse than staying silent.

**Read `context/GLOBAL.md` first** for the whole-system picture (architecture, how the
phases merged, conservative-by-design mechanics, credential status, open risks). Then
`context/phase1.md` / `phase2.md` / `phase3.md` for each phase's full decision log —
every design choice there is written as Decision → Alternatives rejected → Rationale →
Consequence → Reversal cost, and is usually worth reading before changing that area.

## Layout

Backend and frontend are separate trees with **separate virtual environments**. They
never share a venv: the backend carries the Mireye/LLM stack, the frontend only needs a
web server and an HTTP client, and keeping them apart means the console cannot
accidentally import a phase module and bypass the API boundary.

```
backend/                 the three phases — cwd for every backend command
  monitor_records/       Phase 1 (government records → events)
  phase2/                Phase 2 (coordinate → physical datapoints, Mireye)
  phase3/                Phase 3 (event + datapoints → decision) + the combined app
  scripts/run_pipeline.py    end-to-end demo
  tests/fakes/mireye_fake.py fake Mireye backend used by tests and the demo
  .venv/  requirements.txt  .env

frontend/                the operator console — cwd for every frontend command
  app.py                 web server; proxies /api/* to the backend
  static/                index.html, css/, js/ (no build step, plain ES modules)
  .venv/  requirements.txt  .env
```

## Run

Two processes, two terminals. Backend first.

```bash
# backend — from backend/
.venv/Scripts/python.exe -m pytest tests -q                          # offline
.venv/Scripts/python.exe scripts/run_pipeline.py                     # ALERT/SILENCE demo
.venv/Scripts/python.exe -m uvicorn phase3.app:app --reload --port 8000

# frontend — from frontend/
.venv/Scripts/python.exe -m pytest tests -q
.venv/Scripts/python.exe -m uvicorn app:app --reload --port 8080     # console at :8080
```

## Before touching this repo

- Real credentials live in `backend/.env` and `frontend/.env` (both gitignored) — never
  commit either, never echo the raw values back in output. The frontend's Google Maps
  browser key is public by nature and must be restricted by HTTP referrer; it is served
  to the page via `GET /api/config`, never baked into a committed file.
- Run backend commands with cwd `backend/` and frontend commands with cwd `frontend/`.
  The backend resolves `sqlite:///./phase2.db` relative to its cwd.
- `pytest -q` must stay fully offline in both trees — no test should require network or
  a real key.
  `scripts/run_pipeline.py` fakes Mireye on purpose even with a real token configured,
  for reproducibility (`context/phase3.md` D7).
- OpenAI is the default LLM provider, not Gemini — the configured Gemini key is
  free-tier (20 req/day) and gets exhausted quickly (`context/phase1.md` D7 update).
- Only commit when explicitly asked to.
