"""Regression checks for the console's safety-critical static behavior."""

from pathlib import Path


STATIC = Path(__file__).resolve().parents[1] / "static"


def _text(relative: str) -> str:
    return (STATIC / relative).read_text(encoding="utf-8")


def test_static_assets_have_no_nul_bytes():
    for path in STATIC.rglob("*"):
        if path.is_file():
            assert b"\x00" not in path.read_bytes(), path


def test_viewing_a_pairing_does_not_automatically_run_a_paid_decision():
    app = _text("js/app.js")
    assert app.count("API.decide(") == 1
    assert "async function evaluateCurrentPair()" in app
    assert "renderDecisionPending" in app
    assert "await API.vicinity" not in app
    assert "await API.derived" not in app


def test_map_does_not_invent_vicinity_rings():
    map_js = _text("js/map.js")
    index = _text("index.html")
    assert "hasVicinityData ? RING_RADII : []" in map_js
    assert "No vicinity measurement is available" in map_js
    assert 'id="map-vicinity-legend" hidden' in index
