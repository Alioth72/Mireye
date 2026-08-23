"""Feature export for weight calibration.

The weights in `phase2/scoring.py` are **provisional** — chosen by judgement, not fitted.
The plan is to run Phase 2's feature extraction over coordinates of real, existing data
centres, look at what those sites actually have in common, and derive the weights from
that revealed preference instead.

This script produces the input to that work: one flat row per site, carrying **raw field
values alongside the component sub-scores**. Raw values matter — the fitting has to be
able to bypass our bands entirely if the evidence says they are wrong. A file of
sub-scores alone would only ever confirm the judgement it was meant to test.

Uses `/v1/runs` (async `fetch_batch`) so results persist server-side for 30 days and the
CSV artifact comes back index-aligned with no bespoke export code.

    python scripts/calibrate_export.py data/known_datacenters.csv out.csv
    python scripts/calibrate_export.py data/known_datacenters.csv out.csv --sync

Input CSV needs `label,lat,lng`; an optional `is_datacenter` column (1/0) lets you mix in
negative examples, which the fitting needs as much as positives.
"""

from __future__ import annotations

import asyncio
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from phase2 import scoring  # noqa: E402
from phase2.mireye.client import MireyeClient  # noqa: E402
from phase2.mireye.schemas import MireyeError  # noqa: E402

BATCH = 25          # hard cap on locations per batch/run
POLL_SECONDS = 5
MAX_POLLS = 120


def read_sites(path: Path) -> list:
    with path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    out = []
    for r in rows:
        try:
            out.append({
                "label": (r.get("label") or "").strip(),
                "lat": float(r["lat"]),
                "lng": float(r["lng"]),
                "is_datacenter": int(r.get("is_datacenter") or 1),
                "note": (r.get("note") or "").strip(),
            })
        except (KeyError, ValueError):
            print(f"  ! skipping unparseable row: {r}", file=sys.stderr)
    return out


class _Rec:
    """Minimal stand-in so `scoring.score` can consume batch results directly."""

    def __init__(self, name, rec):
        self.field_name = name
        self.value = rec.value
        self.status = rec.status
        self.source = rec.source
        self.source_url = rec.source_url
        self.license = None
        self.confidence = rec.confidence or "unknown"
        self.fetched_at = rec.fetched_at
        self.ttl_seconds = rec.ttl_seconds
        self.notes = rec.notes
        self.unit = rec.unit
        self.dataset_vintage = rec.dataset_vintage
        # store.serialize() reads these on failed fields; a stand-in that omits them
        # blows up exactly when a field fails, which is when you most want the row.
        self.error = getattr(rec, "error", None)
        self.retryable = getattr(rec, "retryable", None)


async def _via_runs(client: MireyeClient, chunk: list, fields: list) -> list:
    """Submit as a run and poll. Caller mistakes fail at SUBMIT, not in the background."""
    locations = [{"lat": s["lat"], "lng": s["lng"]} for s in chunk]
    submitted = await client.submit_run(locations, fields)
    run_id = submitted.get("run_id") or submitted.get("id")
    print(f"    run {run_id} submitted", flush=True)

    for _ in range(MAX_POLLS):
        run = await client.get_run(run_id)
        status = run.get("status")
        if status == "done":
            result = run.get("result") or {}
            return result.get("results") or result.get("locations") or []
        if status == "failed":
            raise MireyeError("run_failed", str(run.get("error")), retryable=True)
        await asyncio.sleep(POLL_SECONDS)
    raise MireyeError("run_timeout", f"run {run_id} still running", retryable=True)


async def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    use_sync = "--sync" in sys.argv
    if len(args) < 2:
        print(__doc__)
        sys.exit(1)

    sites = read_sites(Path(args[0]))
    out_path = Path(args[1])
    fields = scoring.all_feature_fields()
    print(f"{len(sites)} sites x {len(fields)} fields "
          f"({(len(sites) + BATCH - 1) // BATCH} batches)")

    rows: list = []
    async with MireyeClient() as client:
        for start in range(0, len(sites), BATCH):
            chunk = sites[start:start + BATCH]
            print(f"  batch {start // BATCH + 1}: {len(chunk)} sites", flush=True)
            try:
                if use_sync:
                    results = [
                        (i, resp, err)
                        for i, resp, err in await client.fetch_batch(
                            [{"lat": s["lat"], "lng": s["lng"]} for s in chunk], fields
                        )
                    ]
                else:
                    raw = await _via_runs(client, chunk, fields)
                    from phase2.mireye.schemas import FetchResponse
                    results = []
                    for i, entry in enumerate(raw):
                        idx = entry.get("index", i)
                        if entry.get("ok") is False:
                            results.append((idx, None, entry.get("error")))
                        else:
                            body = entry.get("result", entry)
                            results.append((idx, FetchResponse.model_validate(body), None))
            except MireyeError as exc:
                print(f"    ! batch failed: {exc.code} {exc.message}", file=sys.stderr)
                continue

            for index, response, error in results:
                if index >= len(chunk):
                    continue
                site = chunk[index]
                if error is not None:
                    print(f"    ! {site['label']}: {error}", file=sys.stderr)
                    continue
                dps = [_Rec(n, r) for n, r in response.fields.items()]
                rows.append(scoring.feature_row(
                    site["label"], dps,
                    extra={"lat": site["lat"], "lng": site["lng"],
                           "is_datacenter": site["is_datacenter"], "note": site["note"]},
                ))

    if not rows:
        print("no rows produced", file=sys.stderr)
        sys.exit(2)

    columns: list = []
    for row in rows:
        for k in row:
            if k not in columns:
                columns.append(k)

    with out_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=columns)
        w.writeheader()
        for row in rows:
            w.writerow(row)

    print(f"\nwrote {len(rows)} rows x {len(columns)} columns -> {out_path}")
    print("\nColumns are: identity, then one raw value + one __status per field, then "
          "cmp_* component sub-scores, then score_* per metric.")
    print("Fit against the RAW columns, not the cmp_* ones — the sub-scores already "
          "encode the bands this exercise is meant to test.")


if __name__ == "__main__":
    asyncio.run(main())
