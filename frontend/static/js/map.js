/* ============================================================================
   map.js — the vicinity picture.

   This module draws one idea: *what did we actually sample, and how much of it
   is usable?* Everything here exists to keep that honest.

   The original modelling error in this project was conflating the sampled ring
   with the owner's parcel. A 250 / 750 / 1500 m ring is ground we MEASURED
   near a coordinate; it is not a boundary anyone holds. Phase 2's vicinity.py
   was rebuilt around that distinction (connectable vs intrinsic vs regional),
   and this map must not quietly undo it in pixels. So:

     - rings are labelled as sampled ground, never as a parcel;
     - the hull over passing points is captioned with the real fraction from
       the payload, and is not drawn at all when that fraction is missing;
     - synthesised sample positions are announced as canonical, not measured;
     - connectable infrastructure is labelled "reachable, not owned".

   Owned by the map. app.js hands us data; api.js owns the network. We never
   fetch, and we never throw back into the orchestrator — a failure here
   degrades to a caption or to the inline-SVG fallback.
   ========================================================================= */

import { fmtMetres, fmtScore, decisionKey } from "./api.js";

/* ── ring geometry — mirrors phase2/vicinity.py exactly ────────────────────
   Centroid + 8 bearings x 3 rings = 25 locations, which is the hard cap for
   one batch fetch. If the backend ever widens this, the payload's own `rings`
   wins; these are only the defaults we synthesise from.
   ------------------------------------------------------------------------ */
const RING_RADII = [250, 750, 1500];
const BEARING_COUNT = 8;
const BEARING_STEP = 360 / BEARING_COUNT; // 45 degrees, clockwise from north
const M_PER_DEG_LAT = 111_320.0;          // WGS84 mean; ~0.5% at these scales

/* Field names we treat as "connectable infrastructure you reach but do not
   own". Matched as substrings so `nearest_osm_substation_distance_m` and
   `nearest_substation_distance_m` both land. */
const INFRA_DISTANCE = ["nearest_transmission_line_distance_m", "nearest_substation_distance_m"];
const INFRA_VOLTAGE = ["nearest_transmission_line_voltage_kv", "nearest_osm_substation_max_voltage_kv"];

/* ── module state ─────────────────────────────────────────────────────────
   One instance, because there is one map. Every overlay we create is tracked
   here so focusSite() can be called twice without stacking duplicates.
   ------------------------------------------------------------------------ */
const S = {
  ready: false,
  mode: "idle",          // "idle" | "google" | "fallback"
  el: null,
  fallbackEl: null,
  config: null,
  gmaps: null,           // window.google.maps once loaded
  map: null,
  mapIdInUse: null,
  useAdvanced: false,
  theme: null,

  sites: [],
  selectedId: null,
  decisions: new Map(),  // siteId -> "alert" | "review" | "silence"

  markers: new Map(),    // siteId -> marker (advanced element or classic Marker)
  focusOverlays: [],     // everything focusSite() drew, in creation order
  lastFocus: null,       // { site, vicinity, decision } for theme rebuilds
};

/* ══════════════════════════════════════════════════════════════════════════
   colour — always read from tokens.css at runtime.

   The console is light/dark themed and the tokens are redefined in three
   places (bare :root, prefers-color-scheme, [data-theme]). Caching a colour
   across a theme flip would silently strand the map in the old palette, so we
   read on every draw and only cache within a single paint.
   ══════════════════════════════════════════════════════════════════════════ */
function readVar(name) {
  try {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  } catch {
    return "";
  }
}

/** First token in the chain that resolves. Never returns a literal colour. */
function cssVar(...names) {
  for (const n of names) {
    const v = readVar(n);
    if (v) return v;
  }
  return "currentColor";
}

/**
 * Google Maps stylers and Polygon options want a solid colour plus a numeric
 * opacity; --hull-fill is authored as rgba() so the SVG fallback can use it
 * verbatim. Split it rather than inventing a second, drift-prone token.
 */
function splitAlpha(value) {
  const m = /^rgba?\(\s*([\d.]+)[\s,]+([\d.]+)[\s,]+([\d.]+)(?:[\s,/]+([\d.]+%?))?\s*\)$/i.exec(value || "");
  if (!m) return { color: value || "currentColor", opacity: null };
  const hex =
    "#" +
    [m[1], m[2], m[3]]
      .map((n) => Math.max(0, Math.min(255, Math.round(Number(n)))).toString(16).padStart(2, "0"))
      .join("");
  const raw = m[4];
  const opacity = raw == null ? 1 : String(raw).endsWith("%") ? parseFloat(raw) / 100 : parseFloat(raw);
  return { color: hex, opacity: Number.isFinite(opacity) ? opacity : 1 };
}

/** The three decision colours ARE the product vocabulary; undecided is neutral. */
function decisionColour(key) {
  switch (key) {
    case "alert":   return cssVar("--alert");
    case "review":  return cssVar("--review");
    case "silence": return cssVar("--silence");
    default:        return cssVar("--line-strong");
  }
}

/* ══════════════════════════════════════════════════════════════════════════
   captions
   ══════════════════════════════════════════════════════════════════════════ */
const $ = (id) => document.getElementById(id);

function caption(main, sub) {
  const box = $("map-caption");
  const mainEl = $("map-caption-main");
  const subEl = $("map-caption-sub");
  if (mainEl) mainEl.textContent = main || "";
  if (subEl) subEl.textContent = sub || "";
  if (box) box.hidden = !(main || sub);
}

function clearCaption() {
  caption("", "");
}

/** Last resort: say what went wrong where the user is already looking. */
function report(main, sub) {
  caption(main, sub);
}

/* ══════════════════════════════════════════════════════════════════════════
   geometry — the latitude correction is the whole point
   ══════════════════════════════════════════════════════════════════════════ */

/**
 * Offset a coordinate by metres along a compass bearing.
 *
 * Longitude MUST be divided by cos(lat). At Seattle's 47.6 degrees a degree of
 * longitude spans only ~67% of a degree of latitude, so an uncorrected offset
 * walks an ellipse and the "1500 m ring" would really be 1500 m north/south
 * and ~2240 m east/west. Every downstream claim about sampled ground would be
 * wrong by that factor.
 */
function offsetLatLng(lat, lng, metres, bearingDeg) {
  const theta = (bearingDeg * Math.PI) / 180;
  const cosLat = Math.cos((lat * Math.PI) / 180) || 1e-6;
  const dLat = (metres * Math.cos(theta)) / M_PER_DEG_LAT;
  const dLng = (metres * Math.sin(theta)) / (M_PER_DEG_LAT * cosLat);
  return { lat: lat + dLat, lng: lng + dLng };
}

/** Local east/north metres of a point relative to the site. Same correction. */
function toLocalMetres(lat, lng, siteLat, siteLng) {
  const cosLat = Math.cos((siteLat * Math.PI) / 180) || 1e-6;
  return {
    x: (lng - siteLng) * M_PER_DEG_LAT * cosLat, // east
    y: (lat - siteLat) * M_PER_DEG_LAT,          // north
  };
}

/**
 * Monotone-chain convex hull over {x, y} points. Small enough to own; pulling
 * a geometry library in for 20 lines would break the no-build-step rule.
 * A convex hull is affine-invariant, so computing it in local metres gives the
 * same vertices as lat/lng would — the metres just keep the maths readable.
 */
function convexHull(points) {
  const uniq = [];
  const seen = new Set();
  for (const p of points) {
    const k = `${p.x.toFixed(2)}:${p.y.toFixed(2)}`;
    if (!seen.has(k)) { seen.add(k); uniq.push(p); }
  }
  if (uniq.length < 3) return uniq;

  const pts = uniq.slice().sort((a, b) => a.x - b.x || a.y - b.y);
  const cross = (o, a, b) => (a.x - o.x) * (b.y - o.y) - (a.y - o.y) * (b.x - o.x);

  const lower = [];
  for (const p of pts) {
    while (lower.length >= 2 && cross(lower[lower.length - 2], lower[lower.length - 1], p) <= 0) lower.pop();
    lower.push(p);
  }
  const upper = [];
  for (let i = pts.length - 1; i >= 0; i--) {
    const p = pts[i];
    while (upper.length >= 2 && cross(upper[upper.length - 2], upper[upper.length - 1], p) <= 0) upper.pop();
    upper.push(p);
  }
  lower.pop();
  upper.pop();
  const hull = lower.concat(upper);
  return hull.length >= 3 ? hull : uniq;
}

function siteLatLng(site) {
  if (!site) return null;
  const lat = Number(site.lat ?? site.latitude);
  const lng = Number(site.lng ?? site.lon ?? site.longitude);
  if (!Number.isFinite(lat) || !Number.isFinite(lng)) return null;
  return { lat, lng };
}

/* ══════════════════════════════════════════════════════════════════════════
   reading the vicinity payload

   The stored shape (phase2 router `GET /sites/{id}/vicinity`) is:
     { site_id, field_count, by_class: { connectable|intrinsic|regional: [
         { field, class, best, worst, best_at_m, spread, fraction_usable,
           n_answers, n_with_value, coverage_note } ] }, note }
   but a scan response, a flat list, or a field->summary map are all plausible
   and cheap to accept. Be liberal here; the alternative is a blank map.
   ══════════════════════════════════════════════════════════════════════════ */
function summaryList(vicinity) {
  if (!vicinity || typeof vicinity !== "object") return [];
  const out = [];

  const push = (name, agg) => {
    if (!agg || typeof agg !== "object") return;
    out.push({ field: agg.field || agg.field_name || name, ...agg });
  };

  if (Array.isArray(vicinity)) {
    vicinity.forEach((a) => push(a?.field, a));
    return out;
  }
  if (vicinity.by_class && typeof vicinity.by_class === "object") {
    for (const [cls, rows] of Object.entries(vicinity.by_class)) {
      if (Array.isArray(rows)) rows.forEach((a) => push(a?.field, { class: cls, ...a }));
    }
  }
  for (const key of ["summaries", "fields", "by_field", "vicinity"]) {
    const bag = vicinity[key];
    if (Array.isArray(bag)) bag.forEach((a) => push(a?.field, a));
    else if (bag && typeof bag === "object") for (const [n, a] of Object.entries(bag)) push(n, a);
  }
  return out;
}

/** Per-point rows, if the payload carries them at all. */
function sampleRows(vicinity) {
  if (!vicinity || typeof vicinity !== "object") return [];
  for (const key of ["samples", "points", "sample_points", "observations"]) {
    if (Array.isArray(vicinity[key])) return vicinity[key];
  }
  return [];
}

function num(v) {
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

/** Find the first summary whose field name contains any of `needles`. */
function findSummary(list, needles) {
  for (const needle of needles) {
    const hit = list.find((s) => String(s.field || "").includes(needle));
    if (hit) return hit;
  }
  return null;
}

/**
 * The headline intrinsic field — the one whose fraction_usable we caption.
 * Intrinsic fields are properties of the ground itself, which is exactly what
 * "usable sampled ground" means; a connectable field's fraction would be a
 * statement about infrastructure coverage and would mislead here.
 * Deterministic: most sampled points wins, field name breaks ties.
 */
function headlineField(list) {
  const candidates = list
    .filter((s) => num(s.fraction_usable) !== null)
    .sort((a, b) => {
      const ai = a.class === "intrinsic" ? 0 : 1;
      const bi = b.class === "intrinsic" ? 0 : 1;
      if (ai !== bi) return ai - bi;
      const an = num(a.n_with_value) ?? 0;
      const bn = num(b.n_with_value) ?? 0;
      if (an !== bn) return bn - an;
      return String(a.field).localeCompare(String(b.field));
    });
  return candidates[0] || null;
}

/**
 * Decide whether one measured sample counts as good ground.
 * Returns true / false / null — null means undecidable, and an undecidable
 * point is drawn neutral rather than guessed into a pass.
 */
function judgeSample(row, summary) {
  for (const k of ["usable", "passes", "pass", "is_usable"]) {
    if (typeof row?.[k] === "boolean") return row[k];
  }
  const status = String(row?.status || "").toLowerCase();
  if (status === "failed") return null; // withheld is not "nothing here"

  const dir = String(summary?.direction || "").toLowerCase();
  const value = row?.value;
  if (dir === "false_is_best") return !value;
  if (value === null || value === undefined) return null;

  const t = num(summary?.good_threshold);
  const v = num(value);
  if (t !== null && v !== null) {
    if (dir === "min_is_best") return v <= t;
    if (dir === "max_is_best") return v >= t;
  }
  return null;
}

/* ── the canonical 25 positions, when we only have summaries ─────────────── */
function canonicalPositions(lat, lng, rings) {
  const pts = [{ ...offsetLatLng(lat, lng, 0, 0), ring: 0, bearing: 0 }];
  for (const r of rings) {
    for (let i = 0; i < BEARING_COUNT; i++) {
      const bearing = i * BEARING_STEP;
      pts.push({ ...offsetLatLng(lat, lng, r, bearing), ring: r, bearing });
    }
  }
  return pts;
}

/**
 * Build everything the two renderers need, once.
 *
 * The important line in here is `synthesised`. Where the payload gives us real
 * per-point rows we plot measurements. Where it gives us only a summary we
 * plot the CANONICAL ring positions and colour them so the count of passing
 * dots equals the reported fraction — a legend for the number, not a survey.
 * That distinction is stated in the caption every single time.
 */
function buildPlan(site, vicinity, decision) {
  const centre = siteLatLng(site);
  if (!centre) return null;

  const list = summaryList(vicinity);
  const head = headlineField(list);
  const rows = sampleRows(vicinity);

  const explicitRings = Array.isArray(head?.rings)
    ? head.rings.filter((r) => num(r) !== null).map(Number)
    : Array.isArray(vicinity?.rings)
      ? vicinity.rings.filter((r) => num(r) !== null).map(Number)
      : [];
  const hasVicinityData = Boolean(vicinity && (list.length || rows.length));
  const rings = explicitRings.length ? explicitRings : hasVicinityData ? RING_RADII : [];
  const ringRadii = rings.slice().sort((a, b) => a - b);

  const fraction = num(head?.fraction_usable);
  const plan = {
    centre,
    site,
    ringRadii,
    decisionKeyForSite: decision ? decisionKey(decision.decision ?? decision) : null,
    head,
    fraction,
    points: [],
    hull: [],
    infra: null,
    synthesised: false,
    notes: [],
    captionMain: "",
    captionSub: "",
  };

  /* ── layer 3: the 25 sample points ─────────────────────────────────────── */
  const measured = head
    ? rows.filter((r) => {
        const f = r?.field || r?.field_name;
        return (!f || f === head.field) && num(r?.lat) !== null && num(r?.lng) !== null;
      })
    : rows.filter((r) => num(r?.lat) !== null && num(r?.lng) !== null);

  if (measured.length) {
    plan.points = measured.map((r) => ({
      lat: Number(r.lat),
      lng: Number(r.lng),
      ring: num(r.ring_m) ?? 0,
      bearing: num(r.bearing_deg) ?? 0,
      pass: judgeSample(r, head),
      measured: true,
    }));
  } else if (fraction !== null || list.length) {
    plan.synthesised = true;
    const positions = canonicalPositions(centre.lat, centre.lng, ringRadii);
    // Ring order (centroid, then outward) is a presentation convention, not a
    // claim that the inner ground is the good ground. It is deterministic so
    // the same payload always draws the same picture.
    const passCount = fraction === null ? 0 : Math.round(fraction * positions.length);
    plan.points = positions.map((p, i) => ({
      ...p,
      pass: fraction === null ? null : i < passCount,
      measured: false,
    }));
  }

  /* ── layer 4: good-ground hull ─────────────────────────────────────────── */
  if (fraction !== null) {
    const passing = plan.points.filter((p) => p.pass === true);
    const local = passing.map((p) => ({
      ...toLocalMetres(p.lat, p.lng, centre.lat, centre.lng),
      lat: p.lat,
      lng: p.lng,
    }));
    plan.hull = convexHull(local);
    if (plan.hull.length < 3) plan.hull = [];
  }

  /* ── layer 5: connectable infrastructure ───────────────────────────────── */
  const distSummary = findSummary(list, INFRA_DISTANCE);
  const voltSummary = findSummary(list, INFRA_VOLTAGE);
  if (distSummary || voltSummary) {
    const distance = num(distSummary?.best);
    const voltage = num(voltSummary?.best);
    const atM = num(distSummary?.best_at_m) ?? num(voltSummary?.best_at_m);

    // The ring tells us at what radius the best reading appeared, not which
    // way the line runs. If per-point rows exist we can use the real bearing;
    // otherwise the drawn direction is illustrative and we say so.
    let bearing = 45;
    let bearingMeasured = false;
    const distRows = rows.filter((r) => (r?.field || r?.field_name) === distSummary?.field);
    if (distRows.length) {
      const best = distRows
        .filter((r) => num(r?.value) !== null)
        .sort((a, b) => Number(a.value) - Number(b.value))[0];
      if (best && num(best.bearing_deg) !== null) {
        bearing = Number(best.bearing_deg);
        bearingMeasured = true;
      }
    }

    const reach = Math.max(atM || 0, 150);
    if (distance !== null || voltage !== null) {
      plan.infra = {
        end: offsetLatLng(centre.lat, centre.lng, reach, bearing),
        reach,
        bearing,
        bearingMeasured,
        distance,
        voltage,
        // "You reach it, you do not own it" — this is a connectable field, and
        // the wording has to keep that separate from ground we sampled.
        label:
          (voltage !== null ? `${fmtScore(voltage)} kV — ` : "") +
          `${fmtMetres(distance ?? atM)} away · reachable, not owned`,
      };
      if (!bearingMeasured) {
        plan.notes.push("Infrastructure bearing is illustrative — only its distance was measured.");
      }
    }
  }

  /* ── captions ──────────────────────────────────────────────────────────── */
  if (fraction !== null) {
    plan.captionMain = `${Math.round(fraction * 100)}% of sampled ground usable`;
    if (head?.field) plan.captionMain += ` · ${head.field}`;
  } else if (list.length) {
    plan.captionMain = "No usable fraction reported — no ground overlay drawn";
  } else if (vicinity) {
    plan.captionMain = "Vicinity scan returned no field summaries";
  } else {
    plan.captionMain = "No vicinity scan for this site";
  }

  const sub = [];
  if (plan.ringRadii.length) {
    sub.push(
      `Rings are sampled ground at ${plan.ringRadii.join(" / ")} m from the coordinate — ` +
        "the outermost ring is the limit of what we measured, not a parcel boundary."
    );
  } else {
    sub.push("Only the registered coordinate is shown. No vicinity measurement is available, so no sampling rings or coverage area are drawn.");
  }
  if (plan.synthesised) {
    sub.push(
      "Dots are the canonical ring positions (centroid + 8 bearings x 3 rings), not measured locations; " +
        "their pass/fail split reproduces the reported fraction."
    );
  } else if (plan.points.length) {
    sub.push(`${plan.points.length} measured sample points.`);
  }
  if (fraction === null && head?.coverage_note) sub.push(head.coverage_note);
  if (fraction === null && !head) {
    sub.push("fraction_usable is absent, so nothing here may be read as a surveyed boundary.");
  }
  if (head && num(head.best) !== null && num(head.worst) !== null) {
    sub.push(`best ${fmtScore(head.best)} · worst ${fmtScore(head.worst)}.`);
  }
  sub.push(...plan.notes);
  plan.captionSub = sub.join(" ");

  return plan;
}

/* ══════════════════════════════════════════════════════════════════════════
   Google Maps loading
   ══════════════════════════════════════════════════════════════════════════ */

/**
 * Google's published inline bootstrap loader, written out longhand so it can
 * be read and audited. It installs `google.maps.importLibrary`, which is the
 * only supported way to pull `marker` and `geometry` on a weekly channel
 * without a build step.
 */
function injectBootstrap(params) {
  const KEYNAME = "google";
  const CB = "__ib__";
  const w = window;
  const g = (w[KEYNAME] = w[KEYNAME] || {});
  const maps = (g.maps = g.maps || {});
  if (maps.importLibrary) return; // already bootstrapped; loading twice is an error

  const requested = new Set();
  const query = new URLSearchParams();
  let pending = null;

  const load = () =>
    pending ||
    (pending = new Promise((resolve, reject) => {
      const s = document.createElement("script");
      query.set("libraries", [...requested].join(","));
      for (const k of Object.keys(params)) {
        query.set(k.replace(/[A-Z]/g, (t) => "_" + t[0].toLowerCase()), params[k]);
      }
      query.set("callback", `${KEYNAME}.maps.${CB}`);
      s.src = `https://maps.googleapis.com/maps/api/js?${query}`;
      s.nonce = document.querySelector("script[nonce]")?.nonce || "";
      maps[CB] = resolve;
      s.onerror = () => reject(new Error("The Google Maps JavaScript API could not load."));
      document.head.append(s);
    }));

  maps.importLibrary = (name, ...rest) => (requested.add(name), load().then(() => maps.importLibrary(name, ...rest)));
}

function withTimeout(promise, ms, message) {
  return Promise.race([
    promise,
    new Promise((_, reject) => setTimeout(() => reject(new Error(message)), ms)),
  ]);
}

function currentTheme() {
  const explicit = document.documentElement.getAttribute("data-theme");
  if (explicit === "dark" || explicit === "light") return explicit;
  try {
    return window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  } catch {
    return "light";
  }
}

function mapIdFor(theme) {
  const c = S.config || {};
  const id = theme === "dark" ? c.map_id_dark : c.map_id_light;
  return id || c.map_id || null;
}

/**
 * A muted basemap so the data reads. Built FROM the tokens rather than from
 * literals, which means the raster styling flips with the theme for free.
 * Ignored when a cloud Map ID is present — vector maps are styled in the
 * console, and passing both makes Maps log a warning and drop the styles.
 */
function mutedStyles() {
  const ground = cssVar("--ground");
  const surface2 = cssVar("--surface-2");
  const surface3 = cssVar("--surface-3");
  const line = cssVar("--line");
  const muted = cssVar("--muted");
  const accentSoft = cssVar("--accent-soft");
  return [
    { elementType: "geometry", stylers: [{ color: ground }] },
    { elementType: "labels.text.fill", stylers: [{ color: muted }] },
    { elementType: "labels.text.stroke", stylers: [{ color: ground }] },
    { elementType: "labels.icon", stylers: [{ visibility: "off" }] },
    { featureType: "poi", stylers: [{ visibility: "off" }] },
    { featureType: "transit", stylers: [{ visibility: "off" }] },
    { featureType: "administrative", elementType: "geometry.stroke", stylers: [{ color: line }] },
    { featureType: "landscape", elementType: "geometry", stylers: [{ color: ground }] },
    { featureType: "road", elementType: "geometry", stylers: [{ color: surface2 }] },
    { featureType: "road", elementType: "geometry.stroke", stylers: [{ color: line }] },
    { featureType: "road.highway", elementType: "geometry", stylers: [{ color: surface3 }] },
    { featureType: "water", elementType: "geometry", stylers: [{ color: accentSoft }] },
  ];
}

/* ══════════════════════════════════════════════════════════════════════════
   fallback mode — inline SVG, no network, no key
   ══════════════════════════════════════════════════════════════════════════ */
const SVG_SIZE = 400;
const SVG_MARGIN = 26;

function esc(text) {
  return String(text ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}

function showFallback(reason) {
  S.mode = "fallback";
  if (S.el) {
    S.el.hidden = true;
    S.el.style.display = "none"; // console.css is owned elsewhere; force it
  }
  if (S.fallbackEl) {
    S.fallbackEl.hidden = false;
    S.fallbackEl.style.removeProperty("display");
  }
  const note = $("map-fallback-note");
  if (note) note.textContent = reason;
}

/**
 * The whole vicinity picture as inline SVG, on a local equirectangular
 * projection centred on the site. At a 1.5 km radius the distortion of a plane
 * approximation is far below a pixel, and the same cos(lat) correction that
 * keeps the rings circular on the live map keeps them circular here.
 *
 * Every colour is a var() in an inline style, not a presentation attribute:
 * inline styles support custom properties everywhere, so the diagram themes
 * with the rest of the console.
 */
function renderFallbackSvg(plan) {
  const svg = $("map-fallback-svg");
  if (!svg) return;

  if (!plan) {
    svg.innerHTML =
      `<rect x="0" y="0" width="${SVG_SIZE}" height="${SVG_SIZE}" style="fill:var(--surface)"/>` +
      `<text x="${SVG_SIZE / 2}" y="${SVG_SIZE / 2}" text-anchor="middle" ` +
      `style="fill:var(--muted);font-family:var(--font-body);font-size:13px">Select a site to inspect its coordinate.</text>`;
    return;
  }

  const maxR = Math.max(...plan.ringRadii, plan.infra?.reach || 0, 750);
  const scale = (SVG_SIZE / 2 - SVG_MARGIN) / maxR; // px per metre
  const cx = SVG_SIZE / 2;
  const cy = SVG_SIZE / 2;
  const px = (m) => cx + m.x * scale;
  const py = (m) => cy - m.y * scale; // SVG y grows downward; north is up

  const local = (lat, lng) => toLocalMetres(lat, lng, plan.centre.lat, plan.centre.lng);
  const parts = [];

  parts.push(`<rect x="0" y="0" width="${SVG_SIZE}" height="${SVG_SIZE}" style="fill:var(--surface)"/>`);

  // crosshair, so the eye can read bearings off the diagram
  parts.push(
    `<line x1="${cx}" y1="${SVG_MARGIN / 2}" x2="${cx}" y2="${SVG_SIZE - SVG_MARGIN / 2}" ` +
      `style="stroke:var(--line);stroke-width:1"/>` +
      `<line x1="${SVG_MARGIN / 2}" y1="${cy}" x2="${SVG_SIZE - SVG_MARGIN / 2}" y2="${cy}" ` +
      `style="stroke:var(--line);stroke-width:1"/>` +
      `<text x="${cx + 5}" y="${SVG_MARGIN / 2 + 9}" style="fill:var(--muted);font-family:var(--font-mono);font-size:9px">N</text>`
  );

  // ── layer 2: the rings (dashed — sampled ground, not a parcel)
  for (const r of plan.ringRadii) {
    parts.push(
      `<circle cx="${cx}" cy="${cy}" r="${(r * scale).toFixed(2)}" ` +
        `style="fill:none;stroke:var(--ring);stroke-width:1;stroke-dasharray:4 4"/>`
    );
    parts.push(
      `<text x="${cx + 3}" y="${(cy - r * scale - 3).toFixed(2)}" ` +
        `style="fill:var(--muted);font-family:var(--font-mono);font-size:9px">${esc(fmtMetres(r))}</text>`
    );
  }

  // ── layer 4: good-ground hull (only ever drawn from a real fraction)
  if (plan.hull.length >= 3) {
    const pts = plan.hull.map((p) => `${px(p).toFixed(1)},${py(p).toFixed(1)}`).join(" ");
    parts.push(
      `<polygon points="${pts}" style="fill:var(--hull-fill);stroke:var(--hull);stroke-width:1.5"/>`
    );
  }

  // ── layer 5: connectable infrastructure
  if (plan.infra) {
    const end = local(plan.infra.end.lat, plan.infra.end.lng);
    const ex = px(end);
    const ey = py(end);
    parts.push(
      `<line x1="${cx}" y1="${cy}" x2="${ex.toFixed(1)}" y2="${ey.toFixed(1)}" ` +
        `style="stroke:var(--accent);stroke-width:2;stroke-dasharray:6 3"/>`
    );
    parts.push(
      `<circle cx="${ex.toFixed(1)}" cy="${ey.toFixed(1)}" r="3.5" style="fill:var(--accent)"/>`
    );
    const anchor = ex > cx ? "end" : "start";
    const tx = ex > cx ? Math.min(ex, SVG_SIZE - 6) : Math.max(ex, 6);
    parts.push(
      `<text x="${tx.toFixed(1)}" y="${Math.max(12, ey - 8).toFixed(1)}" text-anchor="${anchor}" ` +
        `style="fill:var(--accent);font-family:var(--font-mono);font-size:9px">${esc(plan.infra.label)}</text>`
    );
  }

  // ── neighbouring monitored sites that happen to fall inside the frame
  for (const other of S.sites) {
    if (!other || other.id === plan.site?.id) continue;
    const ll = siteLatLng(other);
    if (!ll) continue;
    const m = local(ll.lat, ll.lng);
    if (Math.hypot(m.x, m.y) > maxR) continue;
    parts.push(
      `<rect x="${(px(m) - 3.5).toFixed(1)}" y="${(py(m) - 3.5).toFixed(1)}" width="7" height="7" ` +
        `style="fill:none;stroke:${cssVarRef(decisionTokenName(S.decisions.get(other.id)))};stroke-width:1.5"/>`
    );
  }

  // ── layer 3: the sample points
  for (const p of plan.points) {
    const m = local(p.lat, p.lng);
    const token = p.pass === true ? "--silence" : p.pass === false ? "--alert" : "--line-strong";
    parts.push(
      `<circle cx="${px(m).toFixed(1)}" cy="${py(m).toFixed(1)}" r="3.2" ` +
        `style="fill:var(${token});fill-opacity:${p.measured ? 1 : 0.72};` +
        `stroke:var(--surface);stroke-width:0.75"/>`
    );
  }

  // ── layer 1: the site pin itself
  const pinToken = decisionTokenName(plan.decisionKeyForSite);
  parts.push(
    `<circle cx="${cx}" cy="${cy}" r="7.5" style="fill:var(${pinToken});stroke:var(--surface);stroke-width:2.5"/>`
  );
  parts.push(
    `<circle cx="${cx}" cy="${cy}" r="10.5" style="fill:none;stroke:var(${pinToken});stroke-width:1.25;stroke-opacity:0.55"/>`
  );

  svg.setAttribute("viewBox", `0 0 ${SVG_SIZE} ${SVG_SIZE}`);
  svg.innerHTML = parts.join("");
}

function decisionTokenName(key) {
  switch (key) {
    case "alert":   return "--alert";
    case "review":  return "--review";
    case "silence": return "--silence";
    default:        return "--line-strong";
  }
}

function cssVarRef(name) {
  return `var(${name})`;
}

/* ══════════════════════════════════════════════════════════════════════════
   overlay bookkeeping — idempotency lives here
   ══════════════════════════════════════════════════════════════════════════ */
function dropOverlay(o) {
  try {
    if (!o) return;
    if (typeof o.setMap === "function") o.setMap(null);
    else if ("map" in o) o.map = null;
    if (typeof o.remove === "function" && o instanceof Element) o.remove();
  } catch {
    /* an overlay that will not detach must not stop the rest detaching */
  }
}

function clearFocusOverlays() {
  for (const o of S.focusOverlays) dropOverlay(o);
  S.focusOverlays = [];
}

function clearMarkers() {
  for (const m of S.markers.values()) dropOverlay(m);
  S.markers.clear();
}

/* ══════════════════════════════════════════════════════════════════════════
   pins
   ══════════════════════════════════════════════════════════════════════════ */
function pinElement(colour, selected) {
  const el = document.createElement("div");
  el.style.width = selected ? "20px" : "15px";
  el.style.height = selected ? "20px" : "15px";
  el.style.borderRadius = "50%";
  el.style.background = colour;
  el.style.boxSizing = "border-box";
  // Selection reads as a heavier ring rather than a different hue: the hue is
  // already spoken for by the decision, and overloading it would lie.
  el.style.border = `${selected ? 4 : 2}px solid ${cssVar("--surface")}`;
  el.style.boxShadow = selected ? `0 0 0 2px ${colour}` : `0 0 0 1px ${cssVar("--line-strong")}`;
  el.style.cursor = "pointer";
  return el;
}

/**
 * app.js owns selection state; we only announce the intent. A CustomEvent on
 * the map element keeps the dependency one-way — importing app.js from here
 * would make the two modules mutually recursive.
 */
function announceSelect(siteId) {
  try {
    (S.el || document.body).dispatchEvent(
      new CustomEvent("monitor:site-select", { detail: { siteId }, bubbles: true })
    );
  } catch {
    /* CustomEvent is universally available; never worth failing a draw over */
  }
}

function drawPins() {
  if (S.mode !== "google" || !S.map || !S.gmaps) return;
  const seen = new Set();

  for (const site of S.sites) {
    const ll = siteLatLng(site);
    if (!ll || !site?.id) continue;
    seen.add(site.id);

    const selected = site.id === S.selectedId;
    const colour = decisionColour(S.decisions.get(site.id));
    const existing = S.markers.get(site.id);
    if (existing) dropOverlay(existing);

    let marker;
    if (S.useAdvanced) {
      marker = new S.gmaps.marker.AdvancedMarkerElement({
        map: S.map,
        position: ll,
        title: site.label || site.address_raw || site.id,
        content: pinElement(colour, selected),
        zIndex: selected ? 40 : 20,
      });
      marker.addListener("gmp-click", () => announceSelect(site.id));
    } else {
      // No Map ID means no AdvancedMarkerElement; classic Marker with a symbol
      // gives the same read without one.
      marker = new S.gmaps.Marker({
        map: S.map,
        position: ll,
        title: site.label || site.address_raw || site.id,
        zIndex: selected ? 40 : 20,
        icon: {
          path: S.gmaps.SymbolPath.CIRCLE,
          scale: selected ? 9 : 6.5,
          fillColor: colour,
          fillOpacity: 1,
          strokeColor: cssVar("--surface"),
          strokeWeight: selected ? 4 : 2,
        },
      });
      marker.addListener("click", () => announceSelect(site.id));
    }
    S.markers.set(site.id, marker);
  }

  for (const [id, marker] of [...S.markers.entries()]) {
    if (!seen.has(id)) {
      dropOverlay(marker);
      S.markers.delete(id);
    }
  }
}

function fitToSites() {
  if (S.mode !== "google" || !S.map || !S.gmaps) return;
  const coords = S.sites.map(siteLatLng).filter(Boolean);
  if (!coords.length) return;
  if (coords.length === 1) {
    S.map.setCenter(coords[0]);
    S.map.setZoom(13);
    return;
  }
  const bounds = new S.gmaps.LatLngBounds();
  coords.forEach((c) => bounds.extend(c));
  S.map.fitBounds(bounds, 48);
}

/* ══════════════════════════════════════════════════════════════════════════
   the live-map rendering of a focused site
   ══════════════════════════════════════════════════════════════════════════ */

/**
 * google.maps.Circle has no dash array, so the ring is a Circle (the geometry
 * of record, as the contract asks) with a Polyline traced over it carrying the
 * dashes. Dashed, not solid, because a solid ring reads as a boundary — and
 * these are sample radii, not anyone's property line.
 */
function drawRing(centre, radius) {
  const ringColour = cssVar("--ring", "--line-strong");

  const circle = new S.gmaps.Circle({
    map: S.map,
    center: centre,
    radius,
    strokeColor: ringColour,
    strokeOpacity: 0.3,
    strokeWeight: 1,
    fillOpacity: 0,
    clickable: false,
    zIndex: 5,
  });
  S.focusOverlays.push(circle);

  const spherical = S.gmaps.geometry?.spherical;
  if (spherical) {
    const path = [];
    for (let a = 0; a <= 360; a += 5) path.push(spherical.computeOffset(centre, radius, a));
    S.focusOverlays.push(
      new S.gmaps.Polyline({
        map: S.map,
        path,
        strokeColor: ringColour,
        strokeOpacity: 0,
        clickable: false,
        zIndex: 6,
        icons: [
          {
            icon: { path: "M 0,-1 0,1", strokeOpacity: 0.9, strokeColor: ringColour, scale: 2 },
            offset: "0",
            repeat: "10px",
          },
        ],
      })
    );
  }
  return circle;
}

function drawFocusOnMap(plan) {
  const g = S.gmaps;

  // ── layer 2: rings
  let outer = null;
  for (const r of plan.ringRadii) outer = drawRing(plan.centre, r);

  // ── layer 3: sample points, as small ground circles so they stay true to
  //    scale at every zoom rather than floating like UI chrome.
  for (const p of plan.points) {
    const token = p.pass === true ? "--silence" : p.pass === false ? "--alert" : "--line-strong";
    S.focusOverlays.push(
      new g.Circle({
        map: S.map,
        center: { lat: p.lat, lng: p.lng },
        radius: 34,
        strokeColor: cssVar("--surface"),
        strokeOpacity: 0.9,
        strokeWeight: 1,
        fillColor: cssVar(token),
        fillOpacity: p.measured ? 0.95 : 0.7,
        clickable: false,
        zIndex: 12,
      })
    );
  }

  // ── layer 4: the good-ground hull
  if (plan.hull.length >= 3) {
    const fill = splitAlpha(cssVar("--hull-fill"));
    S.focusOverlays.push(
      new g.Polygon({
        map: S.map,
        paths: plan.hull.map((p) => ({ lat: p.lat, lng: p.lng })),
        strokeColor: cssVar("--hull"),
        strokeOpacity: 0.9,
        strokeWeight: 1.5,
        fillColor: fill.color,
        fillOpacity: fill.opacity ?? 0.18,
        clickable: false,
        zIndex: 8,
      })
    );
  }

  // ── layer 5: connectable infrastructure
  if (plan.infra) {
    const accent = cssVar("--accent");
    S.focusOverlays.push(
      new g.Polyline({
        map: S.map,
        path: [plan.centre, plan.infra.end],
        strokeColor: accent,
        strokeOpacity: 0,
        clickable: false,
        zIndex: 14,
        icons: [
          {
            icon: { path: "M 0,-1 0,1", strokeOpacity: 1, strokeColor: accent, scale: 3 },
            offset: "0",
            repeat: "12px",
          },
        ],
      })
    );

    if (S.useAdvanced) {
      const tag = document.createElement("div");
      tag.textContent = plan.infra.label;
      tag.style.font = "500 11px/1.3 var(--font-mono, monospace)";
      tag.style.color = accent;
      tag.style.background = cssVar("--surface");
      tag.style.border = `1px solid ${cssVar("--line")}`;
      tag.style.padding = "2px 6px";
      tag.style.whiteSpace = "nowrap";
      S.focusOverlays.push(
        new g.marker.AdvancedMarkerElement({
          map: S.map,
          position: plan.infra.end,
          content: tag,
          zIndex: 45,
        })
      );
    } else {
      S.focusOverlays.push(
        new g.Marker({
          map: S.map,
          position: plan.infra.end,
          zIndex: 45,
          label: { text: plan.infra.label, color: accent, fontSize: "11px", fontWeight: "600" },
          icon: {
            path: g.SymbolPath.CIRCLE,
            scale: 3,
            fillColor: accent,
            fillOpacity: 1,
            strokeWeight: 0,
          },
        })
      );
    }
  }

  // frame the whole sampled area, not just the pin
  try {
    if (outer?.getBounds) S.map.fitBounds(outer.getBounds(), 56);
    else {
      S.map.setCenter(plan.centre);
      S.map.setZoom(14);
    }
  } catch {
    S.map.setCenter(plan.centre);
  }
}

/* ══════════════════════════════════════════════════════════════════════════
   public surface
   ══════════════════════════════════════════════════════════════════════════ */
export const MonitorMap = {
  /** Boot once. Resolves whether or not Google Maps was reachable. */
  async init({ el, fallbackEl, config } = {}) {
    try {
      S.el = el || $("map");
      S.fallbackEl = fallbackEl || $("map-fallback");
      S.config = config || {};
      S.theme = currentTheme();
      S.ready = true;

      if (!S.config.maps_key) {
        showFallback(
          "No Maps API key is configured, so a local coordinate diagram is shown. " +
            "Sampling overlays appear only when real vicinity data is available."
        );
        renderFallbackSvg(null);
        return;
      }

      injectBootstrap({ key: S.config.maps_key, v: "weekly", libraries: "marker,geometry" });

      // A hung script tag is indistinguishable from a slow one; 12 s is well
      // past any healthy load and the fallback is a complete picture anyway.
      const [mapsLib] = await withTimeout(
        Promise.all([
          window.google.maps.importLibrary("maps"),
          window.google.maps.importLibrary("marker").catch(() => null),
          window.google.maps.importLibrary("geometry").catch(() => null),
        ]),
        12_000,
        "Google Maps did not finish loading."
      );

      if (!window.google?.maps || !mapsLib?.Map) throw new Error("google.maps unavailable after load.");
      S.gmaps = window.google.maps;

      buildMap();
      S.mode = "google";
      if (S.el) {
        S.el.hidden = false;
        S.el.style.removeProperty("display");
      }
      if (S.fallbackEl) S.fallbackEl.hidden = true;
    } catch (err) {
      showFallback(
        `The live map is unavailable (${err?.message || "unknown error"}). ` +
          "The local diagram shows the registered coordinate and only the sampling data actually returned by the API."
      );
      try { renderFallbackSvg(S.lastFocus ? buildPlan(S.lastFocus.site, S.lastFocus.vicinity, S.lastFocus.decision) : null); }
      catch { renderFallbackSvg(null); }
    }
  },

  /** Draw or refresh every pin. `decisionsBySite` may be undefined. */
  setSites(sites, selectedId, decisionsBySite) {
    try {
      S.sites = Array.isArray(sites) ? sites.filter(Boolean) : [];
      S.selectedId = selectedId ?? null;
      S.decisions = decisionsBySite instanceof Map ? new Map(decisionsBySite) : new Map();

      if (S.mode === "google") {
        drawPins();
        if (!S.lastFocus) fitToSites();
      } else if (S.mode === "fallback" && S.lastFocus) {
        renderFallbackSvg(buildPlan(S.lastFocus.site, S.lastFocus.vicinity, S.lastFocus.decision));
      }
    } catch (err) {
      report("Could not draw the site pins.", String(err?.message || err));
    }
  },

  /** Centre on a site and draw its whole vicinity picture. Idempotent. */
  async focusSite(site, { vicinity, decision } = {}) {
    try {
      clearFocusOverlays();
      S.lastFocus = { site, vicinity: vicinity ?? null, decision: decision ?? null };

      const centre = siteLatLng(site);
      if (!centre) {
        caption(
          "This site has no usable coordinate",
          "Nothing can be sampled or drawn without a geocode. Re-register the site with an address the geocoder resolves."
        );
        if (S.mode === "fallback") renderFallbackSvg(null);
        return;
      }

      const plan = buildPlan(site, vicinity ?? null, decision ?? null);
      if (!plan) {
        caption("Could not read the vicinity payload", "The scan response did not contain field summaries or sample points.");
        return;
      }
      const vicinityLegend = $("map-vicinity-legend");
      if (vicinityLegend) vicinityLegend.hidden = plan.ringRadii.length === 0;

      caption(plan.captionMain, plan.captionSub);

      if (S.mode === "google" && S.map && S.gmaps) {
        drawPins();
        drawFocusOnMap(plan);
      } else {
        renderFallbackSvg(plan);
      }
    } catch (err) {
      // A drawing failure must never break selection in app.js.
      report(
        "The vicinity could not be drawn.",
        `${err?.message || err}. The decision panel is unaffected.`
      );
    }
  },

  /** Recolour one pin after a decision lands. */
  setDecision(siteId, key) {
    try {
      if (!siteId) return;
      const next = decisionKey(key);
      S.decisions.set(siteId, next);
      if (S.lastFocus?.site?.id === siteId) {
        S.lastFocus.decision = next;
      }
      if (S.mode === "google") drawPins();
      else if (S.lastFocus) renderFallbackSvg(buildPlan(S.lastFocus.site, S.lastFocus.vicinity, S.lastFocus.decision));
    } catch {
      /* a stale pin colour is not worth a thrown error */
    }
  },

  /**
   * Follow the console's theme. A Map ID cannot be changed on a live Map
   * instance, so a light/dark Map ID pair forces a rebuild; otherwise we just
   * repaint, since every colour we use is read from the tokens at draw time.
   */
  setTheme(themeName) {
    try {
      S.theme = themeName === "dark" || themeName === "light" ? themeName : currentTheme();

      if (S.mode === "google" && S.map) {
        const wanted = mapIdFor(S.theme);
        if (wanted !== S.mapIdInUse) {
          clearFocusOverlays();
          clearMarkers();
          buildMap();
        }
        drawPins();
        if (S.lastFocus) {
          clearFocusOverlays();
          const plan = buildPlan(S.lastFocus.site, S.lastFocus.vicinity, S.lastFocus.decision);
          if (plan) drawFocusOnMap(plan);
        }
      } else if (S.mode === "fallback") {
        renderFallbackSvg(S.lastFocus ? buildPlan(S.lastFocus.site, S.lastFocus.vicinity, S.lastFocus.decision) : null);
      }
    } catch {
      /* theme is cosmetic; never let it take the map down */
    }
  },

  /** Drop every overlay and reset the caption. */
  clear() {
    try {
      clearFocusOverlays();
      clearMarkers();
      S.sites = [];
      S.selectedId = null;
      S.decisions = new Map();
      S.lastFocus = null;
      clearCaption();
      if (S.mode === "fallback") renderFallbackSvg(null);
    } catch {
      /* nothing left to degrade to */
    }
  },
};

/* ── map construction, shared by init() and the theme rebuild ─────────────── */
function buildMap() {
  const g = S.gmaps;
  const mapId = mapIdFor(S.theme);
  const first = S.sites.map(siteLatLng).find(Boolean) || { lat: 47.6062, lng: -122.3321 };

  const options = {
    center: first,
    zoom: 12,
    mapId: mapId || undefined,
    disableDefaultUI: true,
    zoomControl: true,
    clickableIcons: false,
    gestureHandling: "greedy",
    backgroundColor: cssVar("--ground"),
  };
  // Styles and a cloud Map ID are mutually exclusive; a vector map is styled
  // in the console, so only the keyless raster path gets the muted JSON.
  if (!mapId) options.styles = mutedStyles();

  S.map = new g.Map(S.el, options);
  S.mapIdInUse = mapId || null;
  // AdvancedMarkerElement requires a Map ID. Without one we fall back to
  // classic Markers rather than silently drawing nothing.
  S.useAdvanced = Boolean(mapId && g.marker?.AdvancedMarkerElement);
}
