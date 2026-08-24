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

- `monitor_records/` — Phase 1 (government records → events)
- `phase2/` — Phase 2 (coordinate → physical datapoints, Mireye)
- `phase3/` — Phase 3 (event + datapoints → decision)
- `tests/fakes/mireye_fake.py` — fake Mireye backend used by tests and the demo by default
- `scripts/run_pipeline.py` — the end-to-end demo

## Run

```bash
pip install -r requirements.txt
pytest -q                          # 124 tests, fully offline
python scripts/run_pipeline.py     # end-to-end ALERT/SILENCE demo
uvicorn phase3.app:app --reload --port 8000   # combined API
```

## Before touching this repo

- Real credentials (Mireye, OpenAI, Gemini) live in `.env` (gitignored) — never commit
  it, never echo the raw values back in output.
- `pytest -q` must stay fully offline — no test should require network or a real key.
  `scripts/run_pipeline.py` fakes Mireye on purpose even with a real token configured,
  for reproducibility (`context/phase3.md` D7).
- OpenAI is the default LLM provider, not Gemini — the configured Gemini key is
  free-tier (20 req/day) and gets exhausted quickly (`context/phase1.md` D7 update).
- Only commit when explicitly asked to.
