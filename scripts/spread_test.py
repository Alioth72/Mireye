"""Build-order step 7 -- the physical discrimination test.

Answers risk R1 in context/phase2.md: *do the physical fields actually diverge across
monitored sites?*

Phase 3's differentiator is "we stayed quiet on non-buildable ground." That only works
if optionality scores separate. If every Seattle site scores high, Phase 3 never emits a
`quiet` and the product is indistinguishable from a keyword feed.

Sites are chosen deliberately for terrain variety, plus two rural King County controls
so we can see what a genuinely different site looks like.

    .venv/Scripts/python.exe scripts/spread_test.py
"""

from __future__ import annotations

import asyncio
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlmodel import Session, select  # noqa: E402

from phase2 import scoring  # noqa: E402
from phase2.db import get_engine, init_db  # noqa: E402
from phase2.models import Site  # noqa: E402
from phase2.mireye.client import MireyeClient  # noqa: E402
from phase2.models import VicinitySummary  # noqa: E402
from phase2.orchestrator import read_or_fetch, scan_vicinity  # noqa: E402

SITES: list[tuple[str, float, float]] = [
    # --- Seattle city limits, chosen for terrain variety ---
    ("Downtown Seattle",        47.6062, -122.3321),
    ("Duwamish industrial",     47.5301, -122.3350),
    ("South Park (floodplain)", 47.5290, -122.3230),
    ("Georgetown industrial",   47.5450, -122.3200),
    ("Interbay (rail/flat)",    47.6420, -122.3790),
    ("West Seattle bluff",      47.5707, -122.3870),
    ("Magnolia bluff",          47.6500, -122.4020),
    ("Northgate",               47.7080, -122.3250),
    # --- rural King County controls ---
    ("Rural KC — Duvall",       47.7420, -121.9860),
    ("Rural KC — Enumclaw",     47.2040, -121.9910),
]

METRIC = sys.argv[1] if len(sys.argv) > 1 else "data_center_optionality"


async def main() -> None:
    init_db()
    engine = get_engine()
    fields = scoring.required_fields(METRIC)
    rows: list[tuple[str, dict]] = []

    async with MireyeClient() as client:
        for label, lat, lng in SITES:
            with Session(engine) as session:
                site = session.exec(
                    select(Site).where(Site.lat == lat, Site.lng == lng)
                ).first()
                if site is None:
                    site = Site(label=label, lat=lat, lng=lng)
                    session.add(site)
                    session.commit()
                    session.refresh(site)

                # Vicinity, not a point. A nearest_* field read at one centroid can
                # only under-report proximity -- that is what made West Seattle read
                # as a false quiet in the first run of this script.
                await scan_vicinity(session, client, site, fields, caller_ref="spread_test")
                answers, outcome = await read_or_fetch(
                    session, client, site, fields, trigger="replay"
                )
                vic = {
                    v.field_name: {
                        "best": v.best, "worst": v.worst, "n_answers": v.n_answers,
                        "fraction_usable": v.fraction_usable, "spread": v.spread,
                    }
                    for v in session.exec(
                        select(VicinitySummary).where(VicinitySummary.site_id == site.id)
                    ).all()
                }
                result = scoring.score(METRIC, answers, vicinity=vic or None)
                rows.append((label, result))
                if outcome.error:
                    print(f"  ! {label}: {outcome.error}", file=sys.stderr)

    # ---- report -----------------------------------------------------------
    cols = [c for c in scoring.DEFAULT_WEIGHTS[METRIC]] + list(scoring.penalties_for(METRIC))
    hdr = f"{'site':<26}{'score':>7}  " + "".join(f"{c[:6]:>8}" for c in cols) + "   basis"
    print(f"metric: {METRIC}  ({len(fields)} fields per location, 25 locations per site)")
    print(hdr)
    print("-" * len(hdr))
    for label, r in sorted(rows, key=lambda x: -x[1]["score"]):
        c = r["components"]
        line = f"{label:<26}{r['score']:>7.3f}  "
        line += "".join(f"{c[k]['score']:>8.2f}" if k in c else f"{'-':>8}" for k in cols)
        print(line + f"   {c['power']['basis']}; {c['clear']['basis']}")

    scores = [r["score"] for _, r in rows]
    seattle = [r["score"] for label, r in rows if not label.startswith("Rural")]
    rural = [r["score"] for label, r in rows if label.startswith("Rural")]

    print()
    print(f"all sites   n={len(scores):<3} min={min(scores):.3f} max={max(scores):.3f} "
          f"spread={max(scores) - min(scores):.3f} stdev={statistics.pstdev(scores):.3f}")
    print(f"Seattle     n={len(seattle):<3} min={min(seattle):.3f} max={max(seattle):.3f} "
          f"spread={max(seattle) - min(seattle):.3f} stdev={statistics.pstdev(seattle):.3f}")
    if rural:
        print(f"rural KC    n={len(rural):<3} min={min(rural):.3f} max={max(rural):.3f}")

    print()
    verdict = (
        "PASS -- scores separate; Phase 3 can produce both alert and quiet"
        if max(seattle) - min(seattle) >= 0.30
        else "FAIL -- this metric does not discriminate across these sites"
    )
    print(f"R1 verdict for {METRIC} (Seattle spread >= 0.30): {verdict}")


if __name__ == "__main__":
    asyncio.run(main())
