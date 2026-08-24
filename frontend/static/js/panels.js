/* ============================================================================
   panels.js — every pixel of DOM outside the map.

   This module renders evidence, not summaries. The whole product claim is that a
   human can check the machine's work in one screen, so the rules here are:

     - Both halves of the evidence are always on screen together. A verdict with
       only the government half is a keyword feed; a verdict with only the
       physical half is a land survey. The pairing is the product.
     - Nothing that distinguishes two different answers is ever collapsed.
       `absent` ("the source looked and found nothing") and `failed` ("the fetch
       broke, we do not know") are different answers and are rendered
       differently everywhere — see Phase 2's store.py header and D7.
     - We never invent precision. A number the backend cannot compute is drawn as
       an empty state with the reason, never as a zero and never as a guess.

   No fetch() lives here; app.js hands this module everything already loaded.
   No innerHTML with backend data lives here either — every node is built with
   createElement/textContent, because passages, basis strings and source URLs all
   originate in scraped documents and third-party APIs.
   ========================================================================= */

import { fmtMetres, fmtScore, fmtDate, decisionKey, statusLabel } from "./api.js";

/* ── tiny DOM helpers ─────────────────────────────────────────────────────── */

const DASH = "—"; // em dash: the one thing we render for "we have no value"
const DOT = " · ";

function byId(id) {
  return document.getElementById(id);
}

/** Empty a container. The only innerHTML in the file, and it interpolates nothing. */
function clearNode(node) {
  if (node) node.innerHTML = "";
}

/** el("span", "cite-lic", "ODbL") — textContent only, never markup. */
function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null && text !== "") node.textContent = String(text);
  return node;
}

/** A present-or-dash string. Renders "—", never the literal "undefined". */
function txt(value) {
  if (value === null || value === undefined) return DASH;
  const s = String(value).trim();
  return s === "" ? DASH : s;
}

/** External links are always noopener/noreferrer: every href here is third-party. */
function link(href, label, cls) {
  const safe = safeHref(href);
  if (!safe) return el("span", cls, txt(label));
  const a = el("a", cls, txt(label));
  a.href = safe;
  a.target = "_blank";
  a.rel = "noopener noreferrer";
  a.title = safe;
  return a;
}

/** Only http(s) survives. Blocks javascript:/data: URLs arriving from a document scrape. */
function safeHref(href) {
  if (!href) return null;
  const s = String(href).trim();
  return /^https?:\/\//i.test(s) ? s : null;
}

function hostOf(href) {
  const safe = safeHref(href);
  if (!safe) return null;
  try {
    return new URL(safe).hostname.replace(/^www\./, "");
  } catch {
    return null;
  }
}

function num(value) {
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

function clampPct(score) {
  const n = num(score);
  if (n === null) return 0;
  return Math.max(0, Math.min(100, n * 100));
}

/** Date + time. fmtDate alone loses the ordering that makes the ledger checkable. */
function fmtWhen(iso) {
  if (!iso) return DASH;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return fmtDate(iso);
  return `${fmtDate(iso)} ${d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })}`;
}

function daysBetween(laterIso, earlierIso) {
  const a = new Date(laterIso).getTime();
  const b = new Date(earlierIso).getTime();
  if (Number.isNaN(a) || Number.isNaN(b)) return null;
  return Math.round((a - b) / 86400000);
}

/** The date the government record actually carries for the stage it is in. */
function stageDate(event) {
  if (!event) return null;
  const stage = String(event.stage || "").toUpperCase();
  if (stage === "ADOPTED" || stage === "REJECTED") {
    return event.adopted_at || event.heard_at || event.introduced_at || null;
  }
  if (stage === "HEARD") return event.heard_at || event.introduced_at || null;
  return event.introduced_at || event.heard_at || event.adopted_at || null;
}

/** A status pill that never collapses `absent` into `failed`. */
function statusPill(status) {
  const label = statusLabel(status);
  const pill = el("span", "status-pill", label.text);
  pill.dataset.tone = label.tone;
  pill.title = label.title;
  return pill;
}

/* ── the gate trace ───────────────────────────────────────────────────────────
   phase3/pipeline.py runs four checks in a fixed order, cheapest first, and a
   SILENCE at any one of them short-circuits the rest:

     1 dedup            one indexed DB read; can only replay, never decide
     2 stage+confidence stage_policy.stage_gate  — free, no DB, no credits
     3 geography        geography.geography_gate — one Site read, no credits
     4 physical         bundle fetch + scoring   — the only step that can cost

   The wire contract does not name the gate that fired, so we infer it. Two
   signals, in order of trust:

     (a) STRUCTURAL — `metric`/`score`/`physical_components` are populated only
         once step 4 actually ran. A stage or geography SILENCE therefore has a
         null metric, by construction, not by convention. Any ALERT can only come
         out of step 4, so an ALERT means all four passed.
     (b) TEXTUAL — the reason strings are written by stage_policy.py and
         geography.py and are matched below. Text is the weaker signal (a wording
         change upstream breaks it), so it is only consulted once (a) has already
         proven the physical step never ran.

   Why this matters enough to be the biggest thing on the panel: "stage is
   PROPOSED, not a decided outcome" and "this ground never had the option" are
   two completely different statements about the world. The first says the
   paperwork is not final and says nothing at all about the site. The second says
   the paperwork IS final, DOES cover this site, and the site's own physical
   profile is the reason there is no alert. A reader who confuses them draws the
   opposite conclusion about the land. So the trace shows all four gates, always,
   and marks exactly which one stopped the pipeline.
   -------------------------------------------------------------------------- */

const GATE_DEDUP = 0;
const GATE_STAGE = 1;
const GATE_GEO = 2;
const GATE_PHYSICAL = 3;

const GATE_DEFS = [
  { key: "dedup", name: "1 · Dedup" },
  { key: "stage", name: "2 · Stage + confidence" },
  { key: "geography", name: "3 · Geography" },
  { key: "physical", name: "4 · Physical evaluation" },
];

// Wording from phase3/stage_policy.py::stage_gate.
const STAGE_PATTERNS = [
  /not a decided outcome/i,
  /alert-eligible/i,
  /\bstage is [a-z]+/i,
  /confidence .*below the/i,
  /keyword-only/i,
];

// Wording from phase3/geography.py::geography_gate, plus pipeline.py's missing-site
// branch — that lookup exists only to feed the geography gate, so it belongs here.
const GEO_PATTERNS = [
  /geographic relevance/i,
  /unresolved/i,
  /jurisdiction/i,
  /km radius/i,
  /km from the event/i,
  /not evaluated in this version/i,
  /no coordinates/i,
  /site .* not found/i,
];

function inferGateTrace(decision) {
  const reasons = Array.isArray(decision?.reasons)
    ? decision.reasons.filter(Boolean).map(String)
    : [];
  const blob = reasons.join(" — ");
  const components = decision?.physical_components;
  const reachedPhysics =
    decision?.metric !== null && decision?.metric !== undefined
      ? true
      : decision?.score !== null && decision?.score !== undefined
        ? true
        : !!(components && typeof components === "object" && Object.keys(components).length);

  const isSilence = decisionKey(decision?.decision) === "silence";

  // An ALERT can only be produced by step 4, so nothing fired.
  if (!isSilence) return { fired: -1, reachedPhysics: true, indeterminate: false, reasons };

  // (a) structural: components/metric present means the bundle was fetched and scored.
  if (reachedPhysics) return { fired: GATE_PHYSICAL, reachedPhysics: true, indeterminate: false, reasons };

  // (b) textual: we already know physics never ran; it is gate 2 or gate 3.
  if (STAGE_PATTERNS.some((re) => re.test(blob))) {
    return { fired: GATE_STAGE, reachedPhysics: false, indeterminate: false, reasons };
  }
  if (GEO_PATTERNS.some((re) => re.test(blob))) {
    return { fired: GATE_GEO, reachedPhysics: false, indeterminate: false, reasons };
  }

  // Neither pattern matched. We still know the true, checkable fact — no bundle was
  // fetched — so we say that and refuse to guess which of the two gates it was.
  return { fired: null, reachedPhysics: false, indeterminate: true, reasons };
}

/** What each gate means when it is the one that stopped the pipeline. */
function firedHeadline(index) {
  switch (index) {
    case GATE_STAGE:
      return "The record is not final. This is a statement about the paperwork — it says nothing about this ground.";
    case GATE_GEO:
      return "The event's geography does not reach this site. Nothing physical was measured here.";
    case GATE_PHYSICAL:
      return "This ground never had the option. The record IS final and it DOES cover this site — the site's own physical profile is why there is no alert.";
    default:
      return null;
  }
}

function passedNote(index, decision, event) {
  switch (index) {
    case GATE_DEDUP:
      return decision?.replayed
        ? "Replayed from the decision store — this (event, stage, confidence bucket, site) key was already decided. Nothing re-evaluated, no credits spent."
        : "First evaluation of this (event, stage, confidence bucket, site) key.";
    case GATE_STAGE: {
      const stage = txt(decision?.stage || event?.stage);
      const conf = num(event?.confidence);
      const confText = conf === null ? "" : ` Confidence ${conf.toFixed(2)} clears the 0.6 bar.`;
      return `Stage ${stage} is a decided outcome (only ADOPTED/REJECTED are alert-eligible).${confText}`;
    }
    case GATE_GEO:
      return "The event's geography covers this site, so a physical evaluation was worth paying for.";
    case GATE_PHYSICAL:
      return "The site's physical profile is material to this event.";
    default:
      return null;
  }
}

function renderGates(decision, event) {
  const trace = inferGateTrace(decision);
  const wrap = el("div", "dp-gates");

  GATE_DEFS.forEach((def, index) => {
    const gate = el("div", "gate");
    gate.dataset.gate = def.key;
    gate.appendChild(el("span", "gate-name", def.name));

    const fired = trace.fired === index;
    const passed = trace.fired === -1 || (trace.fired !== null && index < trace.fired);

    if (fired) {
      gate.classList.add("is-fired");
      gate.appendChild(el("span", "gate-note", firedHeadline(index)));
      for (const reason of trace.reasons) gate.appendChild(el("span", "gate-note", reason));
    } else if (passed) {
      gate.classList.add("is-passed");
      const note = passedNote(index, decision, event);
      if (note) gate.appendChild(el("span", "gate-note", note));
      // An ALERT's reason text belongs on the gate that produced it.
      if (trace.fired === -1 && index === GATE_PHYSICAL) {
        for (const reason of trace.reasons) gate.appendChild(el("span", "gate-note", reason));
      }
    } else if (index === GATE_DEDUP) {
      // Dedup can only replay a stored decision; it never produces one, so it is
      // never "fired" — it is passed-through on every path.
      gate.classList.add("is-passed");
      gate.appendChild(el("span", "gate-note", passedNote(GATE_DEDUP, decision, event)));
    } else {
      gate.appendChild(
        el("span", "gate-note", "Not reached — the pipeline short-circuited before this check. No bundle fetched, no Mireye credits spent.")
      );
    }

    if (trace.indeterminate && index === GATE_STAGE) {
      gate.appendChild(
        el("span", "gate-note", "The reason text did not match a known gate. What is certain: no metric was scored, so the physical step never ran.")
      );
    }

    wrap.appendChild(gate);
  });

  return wrap;
}

/* ── feed ─────────────────────────────────────────────────────────────────── */

function eventId(event) {
  return event?.canonical_id || event?.event_id || null;
}

function decisionsForEvent(decisions, event) {
  const id = eventId(event);
  if (!id || !Array.isArray(decisions)) return [];
  return decisions.filter((d) => d && (d.canonical_id === id || d.event_id === id));
}

/** "3 alert · 7 silence" — a tally a reader can check against the site list. */
function tallyText(rows) {
  if (!rows.length) return "not yet evaluated";
  const counts = { alert: 0, review: 0, silence: 0 };
  for (const d of rows) counts[decisionKey(d.decision)] += 1;
  const parts = [];
  if (counts.alert) parts.push(`${counts.alert} alert`);
  if (counts.review) parts.push(`${counts.review} review`);
  if (counts.silence) parts.push(`${counts.silence} silence`);
  return parts.join(DOT);
}

function worstKey(rows) {
  if (!rows.length) return "none";
  const keys = rows.map((d) => decisionKey(d.decision));
  if (keys.includes("alert")) return "alert";
  if (keys.includes("review")) return "review";
  return "silence";
}

export function renderFeed(events, options = {}) {
  const host = byId("event-feed");
  if (!host) return;
  try {
    renderFeedInner(host, events, options);
  } catch (err) {
    // A render bug must never take the console down; app.js awaits this call.
    console.error("[panels] renderFeed failed", err);
  }
}

function renderFeedInner(host, events, options) {
  const { onSelect, selectedId, filters, decisions } = options;
  clearNode(host);

  const list = Array.isArray(events) ? events : [];
  const stageFilter = String(filters?.stage || "").toUpperCase();
  const showSilenced = filters?.showSilenced !== false;

  for (const event of list) {
    if (!event) continue;
    if (stageFilter && String(event.stage || "").toUpperCase() !== stageFilter) continue;

    const rows = decisionsForEvent(decisions, event);
    const key = worstKey(rows);

    // "Hide silenced" hides events that were evaluated and came back silent
    // everywhere. An event with NO decisions is not silent — it is unevaluated,
    // and hiding it would quietly shrink the record the console claims to show.
    if (!showSilenced && rows.length && key === "silence") continue;

    const item = el("li", "feed-item");
    item.dataset.decision = key;
    const id = eventId(event);
    if (id) item.dataset.id = id;
    if (id && id === selectedId) item.classList.add("is-selected");

    item.tabIndex = 0;
    item.setAttribute("role", "button");
    item.setAttribute("aria-pressed", String(id != null && id === selectedId));

    item.appendChild(el("span", "feed-title", txt(event.title)));

    const meta = el("div", "feed-meta");
    meta.appendChild(el("span", "feed-stage", txt(event.stage)));
    meta.appendChild(el("span", null, txt(event.event_type)));
    meta.appendChild(el("span", "feed-subject", txt(event.subject)));
    meta.appendChild(el("span", null, txt(event.jurisdiction)));
    item.appendChild(meta);

    item.appendChild(el("span", "feed-tally", tallyText(rows)));

    if (typeof onSelect === "function" && id) {
      const fire = () => {
        try {
          onSelect(id);
        } catch (err) {
          console.error("[panels] onSelect(event) threw", err);
        }
      };
      item.addEventListener("click", fire);
      item.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " " || e.key === "Spacebar") {
          e.preventDefault();
          fire();
        }
      });
    }

    host.appendChild(item);
  }
}

/* ── sites ────────────────────────────────────────────────────────────────── */

export function renderSites(sites, options = {}) {
  const host = byId("site-list");
  if (!host) return;
  try {
    renderSitesInner(host, sites, options);
  } catch (err) {
    console.error("[panels] renderSites failed", err);
  }
}

function renderSitesInner(host, sites, options) {
  const { onSelect, selectedId, decisionsBySite } = options;
  clearNode(host);

  const list = Array.isArray(sites) ? sites : [];
  const lookup =
    decisionsBySite instanceof Map
      ? (id) => decisionsBySite.get(id)
      : (id) => (decisionsBySite && typeof decisionsBySite === "object" ? decisionsBySite[id] : undefined);

  for (const site of list) {
    if (!site) continue;
    const item = el("li", "site-item");
    item.dataset.decision = lookup(site.id) || "none";
    if (site.id) item.dataset.id = site.id;
    if (site.id && site.id === selectedId) item.classList.add("is-selected");

    item.tabIndex = 0;
    item.setAttribute("role", "button");
    item.setAttribute("aria-pressed", String(site.id != null && site.id === selectedId));

    item.appendChild(el("span", "site-dot"));

    const body = el("div");
    body.appendChild(el("span", "site-label", txt(site.label || site.address_raw || site.id)));

    const where = [site.political_locality, site.political_region].filter(Boolean).join(", ");
    const sub = el("div", "site-sub", where || txt(site.normalized_address));
    const lat = num(site.lat);
    const lng = num(site.lng);
    const coords = el("code", null, lat === null || lng === null ? DASH : `${lat.toFixed(5)}, ${lng.toFixed(5)}`);
    sub.appendChild(document.createTextNode(DOT));
    sub.appendChild(coords);
    body.appendChild(sub);

    // parcel_grade === false means the geocoder landed somewhere that may belong to
    // the neighbouring parcel — so every physical field for this site may describe
    // the wrong piece of land. Phase 2 exposes this deliberately; a console that
    // swallows it turns a known-uncertain answer into a confident-looking one.
    if (site.degraded === true || site.parcel_grade === false) {
      item.classList.add("site-degraded");
      const note = el(
        "div",
        "site-sub",
        "Coordinate is not parcel-grade — physical fields may describe a neighbouring parcel."
      );
      if (site.precision_note) note.appendChild(el("span", null, ` ${site.precision_note}`));
      body.appendChild(note);
    }

    item.appendChild(body);

    if (typeof onSelect === "function" && site.id) {
      const fire = () => {
        try {
          onSelect(site.id);
        } catch (err) {
          console.error("[panels] onSelect(site) threw", err);
        }
      };
      item.addEventListener("click", fire);
      item.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " " || e.key === "Spacebar") {
          e.preventDefault();
          fire();
        }
      });
    }

    host.appendChild(item);
  }
}

/* ── decision panel ───────────────────────────────────────────────────────── */

export function renderDecisionEmpty(message) {
  const host = byId("decision-panel");
  if (!host) return;
  clearNode(host);
  host.appendChild(el("p", "empty", txt(message || "Select an event and a site to see the decision.")));
}

/** Government half: the passage, the link, the stage and its date. */
function renderRecordSection(decision, event) {
  const section = el("section", "dp-section");
  section.appendChild(el("h3", "dp-section-title", "The record"));

  const evidence = (Array.isArray(decision?.government_evidence) && decision.government_evidence.length
    ? decision.government_evidence
    : Array.isArray(event?.evidence)
      ? event.evidence
      : []
  ).filter(Boolean);

  if (!evidence.length) {
    section.appendChild(
      el("p", "dp-quote", "No passage was captured for this event. The stage below comes from the structured record, not from an extraction.")
    );
  }

  for (const ev of evidence) {
    section.appendChild(el("blockquote", "dp-quote", txt(ev.passage)));
    const label = ev.document_id || hostOf(ev.source_url) || ev.source_url || "source";
    section.appendChild(link(ev.source_url, label, "dp-cite"));
    if (ev.reason) section.appendChild(el("p", "dp-meta", `Why this passage: ${ev.reason}`));
  }

  const stage = txt(decision?.stage || event?.stage);
  const when = stageDate(event);
  const conf = num(event?.confidence);
  const bits = [`Stage ${stage}`, fmtDate(when)];
  if (conf !== null) {
    // Phase 1 R5: this float is not calibrated — 0.90 vs 0.95 is not a real
    // difference. Shown because the gate uses it; qualified so it is not over-read.
    bits.push(`extraction confidence ${conf.toFixed(2)} (uncalibrated)`);
  }
  if (event?.jurisdiction) bits.push(String(event.jurisdiction));
  section.appendChild(el("p", "dp-meta", bits.join(DOT)));

  return section;
}

/** Physical half: components, their basis strings, the composite score. */
function renderGroundSection(decision, derived, vicinity) {
  const section = el("section", "dp-section");
  section.appendChild(el("h3", "dp-section-title", "The ground"));

  const components = decision?.physical_components;
  const entries =
    components && typeof components === "object" && !Array.isArray(components)
      ? Object.entries(components)
      : [];

  if (!entries.length) {
    section.appendChild(
      el(
        "p",
        "dp-meta",
        "No physical evaluation ran for this pairing — the pipeline short-circuited at an earlier gate, so no bundle was fetched and no Mireye credits were spent."
      )
    );
    return section;
  }

  const wrap = el("div", "dp-components");
  for (const [name, raw] of entries) {
    const comp = raw && typeof raw === "object" ? raw : {};
    const row = el("div", "comp");
    row.appendChild(el("span", "comp-name", txt(name)));

    const bar = el("div", "comp-bar");
    const fill = el("div", "comp-fill");
    fill.style.width = `${clampPct(comp.score).toFixed(1)}%`;
    bar.appendChild(fill);
    row.appendChild(bar);

    const weight = num(comp.weight);
    row.appendChild(
      el("span", "comp-val", weight === null ? fmtScore(comp.score) : `${fmtScore(comp.score)} (w ${weight})`)
    );

    // The basis string cites the actual field values that produced the score
    // ("230 kV at nearest line, substation 1200 m away"). It IS the evidence
    // trail — hiding it behind a tooltip would leave only an unfalsifiable number.
    row.appendChild(el("span", "comp-basis", txt(comp.basis)));

    wrap.appendChild(row);
  }
  section.appendChild(wrap);

  const score = el("div", "dp-score");
  score.appendChild(el("span", "dp-metric", txt(decision?.metric)));
  score.appendChild(el("span", null, fmtScore(decision?.score)));
  if (derived && derived.profile) score.appendChild(el("span", "dp-meta", `profile ${txt(derived.profile)}`));
  section.appendChild(score);

  // Composition rule, stated because it is the reason a single weak component can
  // sink the composite (phase2/scoring.py: multiplicative, not additive).
  section.appendChild(
    el("p", "dp-meta", "Composite is a weighted geometric mean — one component at zero takes the whole score to zero.")
  );

  const vNote = vicinityNote(vicinity);
  if (vNote) section.appendChild(el("p", "dp-meta", vNote));

  return section;
}

/** Uses fmtMetres so ring radii read the same here as on the map. */
function vicinityNote(vicinity) {
  if (!vicinity || typeof vicinity !== "object") return null;
  const rings = vicinity.rings || vicinity.radii_m || vicinity.radii;
  const parts = [];
  if (Array.isArray(rings) && rings.length) {
    const radii = rings
      .map((r) => (r && typeof r === "object" ? r.radius_m ?? r.radius ?? r.metres ?? r.m : r))
      .map((r) => fmtMetres(r))
      .filter((s) => s !== DASH);
    if (radii.length) parts.push(`Vicinity scan at ${radii.join(" / ")}`);
  }
  const features = Array.isArray(vicinity.features) ? vicinity.features.length : num(vicinity.count);
  if (features !== null && features !== undefined) parts.push(`${features} feature(s) in range`);
  return parts.length ? parts.join(DOT) : null;
}

/** Normalise whatever provenance shape `derived` arrives in into citation rows. */
function toCitationList(derived) {
  if (!derived || typeof derived !== "object") return [];
  const raw =
    derived.citations ||
    derived.sources ||
    derived.provenance ||
    derived.datapoints ||
    derived.fields ||
    derived.evidence;
  let list = [];
  if (Array.isArray(raw)) list = raw;
  else if (raw && typeof raw === "object") list = Object.values(raw);
  else return [];

  return list
    .filter((c) => c && typeof c === "object")
    .map((c) => ({
      field: c.field || c.field_name || c.name || null,
      value: c.value,
      unit: c.unit,
      status: c.status,
      source: c.source,
      source_url: c.source_url || c.url || null,
      license: c.license || c.licence || null,
      fetched_at: c.fetched_at || c.when || c.as_of || null,
      stale: c.stale === true,
      notes: c.notes || null,
      error: c.error || null,
    }));
}

/** The value cell — the first place absent and failed must not look alike. */
function valueText(cite) {
  const status = String(cite.status || "").toLowerCase();
  // `absent` is a real answer: the source looked and there is nothing there.
  // `failed` is a hole: the fetch broke and we know nothing either way. Showing
  // both as a blank cell would let a reader read a broken fetch as "clear ground",
  // which flips the decision (phase2/scoring.py, _clear_component).
  if (status === "absent") return "nothing here";
  if (status === "failed") return cite.error ? `not known (${cite.error})` : "not known";
  if (cite.value === null || cite.value === undefined) return DASH;
  const v = typeof cite.value === "boolean" ? String(cite.value) : txt(cite.value);
  return cite.unit ? `${v} ${cite.unit}` : v;
}

function renderCitations(derived, decision) {
  const cites = toCitationList(derived);
  const section = el("section", "dp-section");
  section.appendChild(el("h3", "dp-section-title", "Provenance"));

  if (!cites.length) {
    section.appendChild(
      el(
        "p",
        "dp-meta",
        decision?.metric
          ? "Field-level provenance for this score could not be loaded, so the licences behind it are not shown here."
          : "No physical fields were read for this pairing."
      )
    );
    return { section, cites };
  }

  const wrap = el("div", "dp-citations");
  for (const cite of cites) {
    const row = el("div", "cite-row");

    const fieldCell = el("div", "cite-field", txt(cite.field));
    fieldCell.appendChild(document.createTextNode(" "));
    fieldCell.appendChild(el("code", null, valueText(cite)));
    row.appendChild(fieldCell);

    row.appendChild(statusPill(cite.status));

    const srcLabel = cite.source || hostOf(cite.source_url) || DASH;
    row.appendChild(link(cite.source_url, srcLabel, "cite-src"));

    // The licence is rendered as text, not left implicit in the URL. Several of
    // these sources are ODbL, whose share-alike reaches derived values, and an
    // alert built on them is redistribution (phase2/mireye/licenses.py). A reader
    // who cannot see the licence cannot tell what they are allowed to forward.
    row.appendChild(el("span", "cite-lic", txt(cite.license)));

    const when = el("span", "cite-when", fmtDate(cite.fetched_at));
    if (cite.stale) when.title = "past its TTL — fetched_at is the authoritative as-of";
    row.appendChild(when);

    wrap.appendChild(row);
  }
  section.appendChild(wrap);
  return { section, cites };
}

function renderCaveats(host, { decision, event, derived, vicinity, cites }) {
  const caveats = [];

  // 1. Provisional calibration — the weights and the 0.5 threshold are placeholders
  //    until they are fitted against real sites (context/phase3.md R1).
  const cal = derived?.calibration ?? derived?.profile_calibration ?? null;
  const calText = typeof cal === "string" ? cal : cal && typeof cal === "object" ? cal.status || cal.state || cal.note || "" : "";
  if (/provisional|placeholder|uncalibrated|unvalidated|untuned|not fitted/i.test(String(calText))) {
    caveats.push(
      `Scoring is provisional (${String(calText)}). The component weights and the alert threshold are placeholders, not values fitted against real sites — read the ordering, not the absolute number.`
    );
  }

  // 2. As-of exposure. Mireye answers as-of-now, never as-of-event. If the fields
  //    were fetched long after the government record's own date, the score
  //    describes today's ground, not the ground as it stood when the vote happened.
  const when = stageDate(event);
  const latest = (cites || [])
    .map((c) => c.fetched_at)
    .filter(Boolean)
    .sort()
    .pop();
  if (when && latest) {
    const gap = daysBetween(latest, when);
    if (gap !== null && gap > 30) {
      caveats.push(
        `As-of exposure: physical fields were fetched ${gap} days after the record's ${fmtDate(when)} date. Mireye answers as-of-now, not as-of-event, so this describes the ground today.`
      );
    }
  }

  // 3. Point vs vicinity. A single coordinate is not the parcel.
  const measurement = String(derived?.measurement || vicinity?.measurement || "").toLowerCase();
  if (measurement === "point") {
    caveats.push(
      "Measured at a point, not across a vicinity. Every field describes one coordinate; a feature just outside it is invisible to this score."
    );
  } else if (!measurement && !vicinity && decision?.metric) {
    caveats.push(
      "No vicinity scan is attached to this decision, so every field was measured at the single site coordinate rather than across the surrounding ground."
    );
  }

  for (const text of caveats) host.appendChild(el("p", "dp-caveat", text));
}

export function renderDecision(decision, context = {}) {
  const host = byId("decision-panel");
  if (!host) return;

  try {
    if (!decision) {
      renderDecisionEmpty("No decision is available for this pairing.");
      return;
    }
    const { event, site, derived, vicinity } = context;
    clearNode(host);

    const dp = el("div", "dp");

    /* head */
    const head = el("div", "dp-head");
    const key = decisionKey(decision.decision);
    const verdict = el("span", "dp-verdict", String(decision.decision || key).toUpperCase());
    verdict.dataset.decision = key;
    head.appendChild(verdict);
    head.appendChild(el("span", "dp-site", txt(site?.label || site?.address_raw || decision.site_id)));
    const meta = [];
    if (decision.evaluated_at) meta.push(`evaluated ${fmtWhen(decision.evaluated_at)}`);
    meta.push(decision.replayed ? "replayed (stored decision)" : "fresh evaluation");
    head.appendChild(el("span", "dp-meta", meta.join(DOT)));
    dp.appendChild(head);

    /* the gate trace — the single most load-bearing thing on this panel */
    dp.appendChild(renderGates(decision, event));

    /* both halves of the evidence, always both */
    dp.appendChild(renderRecordSection(decision, event));
    dp.appendChild(renderGroundSection(decision, derived, vicinity));

    const { section: citeSection, cites } = renderCitations(derived, decision);
    dp.appendChild(citeSection);

    // A withheld field is a gap in the evidence, not a zero in it. Called out
    // separately from `absent` so a reader never reads "we could not look" as
    // "there is nothing there".
    const failed = (cites || []).filter((c) => String(c.status || "").toLowerCase() === "failed");
    if (failed.length) {
      dp.appendChild(
        el(
          "p",
          "dp-caveat",
          `${failed.length} field(s) were withheld — the fetch errored, so these are gaps in the evidence, not findings of "nothing here": ${failed
            .map((c) => txt(c.field))
            .join(", ")}.`
        )
      );
    }

    renderCaveats(dp, { decision, event, derived, vicinity, cites });

    host.appendChild(dp);
  } catch (err) {
    // Never throw back into app.js: a render bug must not take the console down.
    console.error("[panels] renderDecision failed", err);
    renderDecisionEmpty("This decision could not be rendered. See the console for detail.");
  }
}

/* ── ledger ───────────────────────────────────────────────────────────────── */

function creditsOf(row) {
  const charged = num(row?.charged_credits);
  if (charged !== null) return charged;
  const quoted = num(row?.quoted_credits);
  return quoted === null ? 0 : quoted;
}

function budgetRemaining(budget) {
  if (!budget || typeof budget !== "object") return null;
  for (const key of ["credits_remaining", "remaining_credits", "remaining", "credits", "balance"]) {
    const v = num(budget[key]);
    if (v !== null) return v;
  }
  const nested = budget.usage || budget.data;
  if (nested && typeof nested === "object") return budgetRemaining(nested);
  return null;
}

export function renderLedger(rows, budget) {
  const host = byId("ledger-table");
  const headline = byId("ledger-headline");

  try {
    const list = (Array.isArray(rows) ? rows : []).filter(Boolean);

    // Grouped by caller_ref because that column carries Phase 3's canonical event
    // id. Grouping is what makes the one-fetch-per-event claim checkable by eye:
    // a group with five rows in it is the claim failing, on screen, unarguably.
    const groups = new Map();
    for (const row of list) {
      const key = row.caller_ref || " unattributed";
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(row);
    }

    if (host) {
      clearNode(host);
      const table = el("div", "ledger");

      const head = el("div", "ledger-head");
      for (const label of ["Trigger", "Fields", "Quoted", "Charged", "When"]) {
        head.appendChild(el("span", null, label));
      }
      table.appendChild(head);

      if (!list.length) {
        table.appendChild(el("p", "empty", "No fetches logged yet."));
      }

      for (const [key, groupRows] of groups) {
        const group = el("div", "ledger-group");
        const unattributed = key === " unattributed";
        const credits = groupRows.reduce((sum, r) => sum + creditsOf(r), 0);
        const hd = el(
          "div",
          "ledger-group-hd",
          `${unattributed ? "no caller ref (not attributed to an event)" : key}${DOT}${groupRows.length} fetch(es)${DOT}${credits} credits`
        );
        group.appendChild(hd);

        for (const row of groupRows) {
          const tr = el("div", "ledger-row");
          const fields = Array.isArray(row.fields) ? row.fields : [];
          const quoted = num(row.quoted_credits);

          // A null quote means the fetch went out without a priced quote first —
          // the ledger cannot prove what that call was expected to cost. Surfaced
          // rather than dashed away, because it is a real defect in the audit trail.
          if (quoted === null) {
            tr.classList.add("led-unquoted");
            tr.title = "This fetch was not quoted before it went out — its expected cost was never recorded.";
          }
          if (row.ok === false) tr.dataset.failed = "true";

          tr.appendChild(el("span", "led-trigger", txt(row.trigger)));
          const fieldCell = el("span", "led-fields", `${fields.length} field(s)`);
          if (fields.length) fieldCell.title = fields.join(", ");
          tr.appendChild(fieldCell);
          tr.appendChild(el("span", "led-quoted", quoted === null ? "not quoted" : String(quoted)));
          tr.appendChild(el("span", "led-charged", num(row.charged_credits) === null ? DASH : String(num(row.charged_credits))));
          tr.appendChild(el("span", "led-when", fmtWhen(row.created_at)));
          if (row.error) tr.appendChild(el("span", null, String(row.error)));

          group.appendChild(tr);
        }

        table.appendChild(group);
      }

      host.appendChild(table);
    }

    if (headline) {
      const events = [...groups.keys()].filter((k) => k !== " unattributed").length;
      const credits = list.reduce((sum, r) => sum + creditsOf(r), 0);
      const remaining = budgetRemaining(budget);
      headline.textContent =
        `${events} events${DOT}${list.length} fetches${DOT}${credits} credits` +
        (remaining === null ? "" : `${DOT}${remaining} credits remaining`);
    }

    // The topbar readout has no other owner and the budget payload arrives here.
    const readout = byId("budget-readout");
    if (readout) {
      const remaining = budgetRemaining(budget);
      readout.textContent = remaining === null ? DASH : `${remaining} credits`;
    }
  } catch (err) {
    console.error("[panels] renderLedger failed", err);
    if (host) {
      clearNode(host);
      host.appendChild(el("p", "empty", "The ledger could not be rendered."));
    }
    if (headline) headline.textContent = DASH;
  }
}

/* ── replay ───────────────────────────────────────────────────────────────── */

function statTile(value, label) {
  const tile = el("div", "replay-stat");
  tile.appendChild(el("span", "replay-stat-v", value === null || value === undefined ? DASH : String(value)));
  tile.appendChild(el("span", "replay-stat-l", label));
  return tile;
}

function timeOf(value) {
  if (!value) return null;
  const t = new Date(value).getTime();
  return Number.isNaN(t) ? null : t;
}

function pickDate(run, keys) {
  for (const k of keys) {
    if (run && run[k]) return run[k];
  }
  return null;
}

function renderTimeline(host, corpus) {
  const runs = corpus.filter((r) => r && typeof r === "object");
  const stamps = [];
  const shaped = runs.map((run) => {
    const alert = pickDate(run, ["alerted_at", "alert_at", "decided_at", "evaluated_at"]);
    const adopt = pickDate(run, ["adopted_at", "adoption_at", "adoption_date"]);
    const press = pickDate(run, ["first_press_at", "press_at", "first_coverage_at"]);
    for (const v of [alert, adopt, press]) {
      const t = timeOf(v);
      if (t !== null) stamps.push(t);
    }
    return { run, alert, adopt, press };
  });

  if (!stamps.length) return false;
  const min = Math.min(...stamps);
  const max = Math.max(...stamps);
  const span = max - min || 1;
  const pos = (v) => {
    const t = timeOf(v);
    return t === null ? null : ((t - min) / span) * 100;
  };

  for (const { run, alert, adopt, press } of shaped) {
    const row = el("div", "tl-row");
    row.appendChild(el("span", "tl-label", txt(run.title || run.canonical_id || run.id)));
    const track = el("div", "tl-track");

    const aPos = pos(alert);
    const dPos = pos(adopt);
    const pPos = pos(press);

    // The gap is the claim: how much ground lies between the alert and the vote.
    if (aPos !== null && dPos !== null) {
      const gap = el("span", "tl-gap");
      gap.style.left = `${Math.min(aPos, dPos).toFixed(2)}%`;
      gap.style.width = `${Math.abs(dPos - aPos).toFixed(2)}%`;
      const days = daysBetween(adopt, alert);
      if (days !== null) gap.title = `${days} days between alert and adoption`;
      track.appendChild(gap);
    }

    const marker = (cls, left, label, iso) => {
      if (left === null) return;
      const m = el("span", cls);
      m.style.left = `${left.toFixed(2)}%`;
      m.title = `${label}: ${fmtDate(iso)}`;
      track.appendChild(m);
    };
    marker("tl-alert", aPos, "alerted", alert);
    marker("tl-adopt", dPos, "adopted", adopt);
    marker("tl-press", pPos, "first press", press);

    row.appendChild(track);
    host.appendChild(row);
  }
  return true;
}

export function renderReplay(runs, errorMessage) {
  const summary = byId("replay-summary");
  const timeline = byId("replay-timeline");
  const misses = byId("replay-misses");

  try {
    clearNode(summary);
    clearNode(timeline);
    clearNode(misses);

    if (errorMessage || !runs || typeof runs !== "object") {
      if (timeline) {
        timeline.appendChild(
          el("p", "replay-empty", txt(errorMessage || "The replay scorecard is not available."))
        );
      }
      return;
    }

    /* Only the counts the endpoint actually derived from stored decisions. There
       is deliberately no precision, recall or lead-time tile here: those need
       ground-truth labels and a replay corpus that do not exist, and a fabricated
       number on a scorecard is worse than a missing one because a reader cannot
       tell the two apart once it is rendered. */
    if (summary) {
      summary.appendChild(statTile(runs.total_decisions, "decisions stored"));
      summary.appendChild(statTile(runs.distinct_canonical_ids, "government actions covered"));
      const byDecision = runs.by_decision && typeof runs.by_decision === "object" ? runs.by_decision : {};
      for (const [k, v] of Object.entries(byDecision)) {
        summary.appendChild(statTile(v, `${String(k).toLowerCase()} decisions`));
      }
      const byStage = runs.by_stage && typeof runs.by_stage === "object" ? runs.by_stage : {};
      for (const [k, v] of Object.entries(byStage)) {
        summary.appendChild(statTile(v, `stage ${String(k).toLowerCase()}`));
      }
    }

    const corpus = Array.isArray(runs.corpus) ? runs.corpus : null;
    const drew = corpus && corpus.length && timeline ? renderTimeline(timeline, corpus) : false;

    if (!drew && timeline) {
      timeline.appendChild(
        el(
          "p",
          "replay-empty",
          txt(
            runs.note ||
              "No replay corpus exists yet. Lead time versus adoption and versus first press coverage cannot be computed until a corpus carrying adoption dates and press dates exists."
          )
        )
      );
    }

    if (!drew && misses) {
      misses.appendChild(
        el(
          "p",
          "replay-empty",
          "Missed events cannot be listed either: naming a miss needs ground-truth labels the system has never been given. Nothing is estimated in their place."
        )
      );
    }
  } catch (err) {
    console.error("[panels] renderReplay failed", err);
    if (timeline) {
      clearNode(timeline);
      timeline.appendChild(el("p", "replay-empty", "The replay scorecard could not be rendered."));
    }
  }
}

/* ── toast ────────────────────────────────────────────────────────────────── */

let toastHideTimer = null;
let toastUnmountTimer = null;

export function toast(message, tone) {
  const host = byId("toast");
  if (!host) return;
  try {
    clearTimeout(toastHideTimer);
    clearTimeout(toastUnmountTimer);

    host.textContent = txt(message);
    host.dataset.tone = String(tone || "review").toLowerCase();
    host.hidden = false;
    // Next frame, so the class change is a transition and not a paint.
    requestAnimationFrame(() => host.classList.add("is-visible"));

    toastHideTimer = setTimeout(() => {
      host.classList.remove("is-visible");
      // Stay in the tree briefly so the fade-out can run before it is hidden.
      toastUnmountTimer = setTimeout(() => {
        host.hidden = true;
      }, 300);
    }, 4000);
  } catch (err) {
    console.error("[panels] toast failed", err);
  }
}

/* ── the exported surface app.js binds against ────────────────────────────── */

export const Panels = {
  renderFeed,
  renderSites,
  renderDecision,
  renderDecisionEmpty,
  renderLedger,
  renderReplay,
  toast,
};

export default Panels;
