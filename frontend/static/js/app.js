/* ============================================================================
   app.js — the orchestrator. Owns state and wiring; owns no rendering.

   Module contract (do not change without updating both implementations):

     map.js    export const MonitorMap = {
                 async init({ el, fallbackEl, config }),   // once, at boot
                 setSites(sites, selectedId),              // draw/refresh all pins
                 async focusSite(site, { vicinity, decision }), // centre + rings + hull
                 setDecision(siteId, decisionKey),         // recolour one pin
                 clear(),
               }

     panels.js export const Panels = {
                 renderFeed(events, { onSelect, selectedId, filters }),
                 renderSites(sites, { onSelect, selectedId, decisionsBySite }),
                 renderDecision(decision, { event, site, derived, vicinity }),
                 renderDecisionEmpty(message),
                 renderLedger(rows, budget),
                 renderReplay(runs),
                 toast(message, tone),
               }
   ========================================================================= */

import { API, ApiError, decisionKey } from "./api.js";
import { MonitorMap } from "./map.js";
import { Panels } from "./panels.js";

const state = {
  config: null,
  events: [],
  sites: [],
  decisions: [],
  selectedEventId: null,
  selectedSiteId: null,
  filters: { stage: "", showSilenced: true },
};

let eventLoadRevision = 0;
let evaluatingKey = null;

const $ = (id) => document.getElementById(id);

/* ── decisions indexed by site for the current event ─────────────────────── */
function decisionsForSelectedEvent() {
  const out = new Map();
  if (!state.selectedEventId) return out;
  for (const d of state.decisions) {
    if (d.canonical_id === state.selectedEventId || d.event_id === state.selectedEventId) {
      out.set(d.site_id, decisionKey(d.decision));
    }
  }
  return out;
}

function currentEvent() {
  return state.events.find(
    (e) => e.canonical_id === state.selectedEventId || e.event_id === state.selectedEventId
  ) || null;
}

function currentSite() {
  return state.sites.find((s) => s.id === state.selectedSiteId) || null;
}

/* ── rendering passes ────────────────────────────────────────────────────── */
function paintFeed() {
  const visibleCount = Panels.renderFeed(state.events, {
    onSelect: selectEvent,
    selectedId: state.selectedEventId,
    filters: state.filters,
    decisions: state.decisions,
  });
  $("feed-count").textContent = String(visibleCount);
  $("feed-empty").hidden = visibleCount > 0;
  $("feed-empty").textContent = state.events.length
    ? "No events match the current filters."
    : "No events yet. Run the ingest to populate the record.";
}

function paintSites() {
  const byDecision = decisionsForSelectedEvent();
  Panels.renderSites(state.sites, {
    onSelect: selectSite,
    selectedId: state.selectedSiteId,
    decisionsBySite: byDecision,
  });
  $("site-count").textContent = String(state.sites.length);
  MonitorMap.setSites(state.sites, state.selectedSiteId, byDecision);
}

/* ── selection ───────────────────────────────────────────────────────────── */
async function selectEvent(eventId) {
  const revision = ++eventLoadRevision;
  state.selectedEventId = eventId;
  paintFeed();
  if (state.selectedSiteId) Panels.renderDecisionEmpty("Loading the stored decision…");

  await loadDecisions({ canonical_id: eventId });
  if (revision !== eventLoadRevision || state.selectedEventId !== eventId) return;

  paintSites();
  showDecision();
}

async function selectSite(siteId) {
  state.selectedSiteId = siteId;
  paintSites();

  const site = currentSite();
  if (!site) return;
  await MonitorMap.focusSite(site, { vicinity: null, decision: decisionForSite(siteId) });
  showDecision();
}

function decisionForSite(siteId) {
  return (
    state.decisions.find(
      (d) =>
        d.site_id === siteId &&
        (d.canonical_id === state.selectedEventId || d.event_id === state.selectedEventId)
    ) || null
  );
}

function mergeDecisions(rows) {
  const incoming = Array.isArray(rows) ? rows.filter(Boolean) : [];
  if (!incoming.length) return;
  const replaced = new Set(
    incoming.map((d) => `${d.canonical_id || d.event_id}|${d.stage}|${d.site_id}`)
  );
  state.decisions = [
    ...incoming,
    ...state.decisions.filter(
      (d) => !replaced.has(`${d.canonical_id || d.event_id}|${d.stage}|${d.site_id}`)
    ),
  ];
}

function showDecision() {
  const site = currentSite();
  const event = currentEvent();
  if (!site || !event) {
    Panels.renderDecisionEmpty("Select an event and a site to see the decision.");
    return;
  }

  const decision = decisionForSite(site.id);
  if (!decision) {
    Panels.renderDecisionPending({ event, site, onEvaluate: evaluateCurrentPair });
    return;
  }

  Panels.renderDecision(decision, {
    event,
    site,
    derived: null,
    vicinity: null,
  });
  MonitorMap.setDecision(site.id, decisionKey(decision.decision));
}

async function evaluateCurrentPair() {
  const site = currentSite();
  const event = currentEvent();
  if (!site || !event) return;

  const eventId = state.selectedEventId;
  const siteId = site.id;
  const key = `${eventId}|${siteId}`;
  if (evaluatingKey === key) return;

  evaluatingKey = key;
  Panels.renderDecisionPending({ event, site, busy: true });
  try {
    const decision = await API.decide({ event, site_id: siteId });
    mergeDecisions([decision]);
    if (state.selectedEventId === eventId && state.selectedSiteId === siteId) {
      MonitorMap.setDecision(siteId, decisionKey(decision.decision));
      paintFeed();
      paintSites();
      showDecision();
    }
  } catch (err) {
    if (state.selectedEventId === eventId && state.selectedSiteId === siteId) {
      Panels.renderDecisionPending({
        event,
        site,
        onEvaluate: evaluateCurrentPair,
        error: err instanceof ApiError ? `Could not evaluate: ${err.message}` : "Could not evaluate this pairing.",
      });
    }
  } finally {
    if (evaluatingKey === key) evaluatingKey = null;
  }
}

/* ── loaders ─────────────────────────────────────────────────────────────── */
async function loadDecisions(filters = {}, { replace = false } = {}) {
  try {
    const res = await API.decisions(filters);
    const rows = Array.isArray(res) ? res : res?.decisions || [];
    if (replace) state.decisions = rows;
    else mergeDecisions(rows);
  } catch {
    // A failed read must not erase decisions already visible in the console.
  }
}

async function loadWatch() {
  const [events, sites, decisions] = await Promise.allSettled([
    API.events(),
    API.sites(),
    API.decisions(),
  ]);
  state.events = events.status === "fulfilled" ? (events.value || []) : [];
  state.sites  = sites.status  === "fulfilled" ? (sites.value  || []) : [];
  state.decisions = decisions.status === "fulfilled"
    ? (Array.isArray(decisions.value) ? decisions.value : decisions.value?.decisions || [])
    : [];

  if (events.status === "rejected") Panels.toast("Could not load the public record.", "alert");
  if (sites.status === "rejected")  Panels.toast("Could not load monitored sites.", "alert");
  if (decisions.status === "rejected") Panels.toast("Could not load stored decisions.", "review");

  paintFeed();
  paintSites();
}

async function loadLedger() {
  const [rows, budget] = await Promise.allSettled([API.fetchLog(), API.budget()]);
  Panels.renderLedger(
    rows.status === "fulfilled" ? (rows.value?.entries || rows.value || []) : [],
    budget.status === "fulfilled" ? budget.value : null
  );
}

async function loadReplay() {
  try {
    Panels.renderReplay(await API.replayRuns());
  } catch (err) {
    Panels.renderReplay(null, err instanceof ApiError ? err.message : "Replay is not available yet.");
  }
}

/* ── tabs ────────────────────────────────────────────────────────────────── */
const VIEWS = [
  ["tab-watch",  "view-watch",  null],
  ["tab-replay", "view-replay", loadReplay],
  ["tab-ledger", "view-ledger", loadLedger],
];

async function activateView(index, { focus = false } = {}) {
  const [, , loader] = VIEWS[index];
  for (let i = 0; i < VIEWS.length; i += 1) {
    const [tabId, viewId] = VIEWS[i];
    const active = i === index;
    $(tabId).classList.toggle("is-active", active);
    $(tabId).setAttribute("aria-selected", String(active));
    $(tabId).tabIndex = active ? 0 : -1;
    $(viewId).hidden = !active;
    $(viewId).classList.toggle("is-active", active);
  }
  if (focus) $(VIEWS[index][0]).focus();
  if (loader) {
    const tab = $(VIEWS[index][0]);
    tab.setAttribute("aria-busy", "true");
    try { await loader(); }
    finally { tab.removeAttribute("aria-busy"); }
  }
}

function wireTabs() {
  VIEWS.forEach(([tabId], index) => {
    const tab = $(tabId);
    tab.addEventListener("click", () => activateView(index));
    tab.addEventListener("keydown", (event) => {
      let next = null;
      if (event.key === "ArrowRight") next = (index + 1) % VIEWS.length;
      if (event.key === "ArrowLeft") next = (index - 1 + VIEWS.length) % VIEWS.length;
      if (event.key === "Home") next = 0;
      if (event.key === "End") next = VIEWS.length - 1;
      if (next !== null) {
        event.preventDefault();
        activateView(next, { focus: true });
      }
    });
  });
}

/* ── theme ───────────────────────────────────────────────────────────────── */
function wireTheme() {
  const root = document.documentElement;
  const button = $("theme-toggle");
  const updateControl = () => {
    const active = root.getAttribute("data-theme") ||
      (window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    const target = active === "dark" ? "light" : "dark";
    button.setAttribute("aria-label", `Switch to ${target} theme`);
    button.title = `Switch to ${target} theme`;
  };

  let stored = null;
  try { stored = localStorage.getItem("monitor-theme"); } catch { /* private mode */ }
  if (stored === "dark" || stored === "light") root.setAttribute("data-theme", stored);
  updateControl();

  button.addEventListener("click", () => {
    const now = root.getAttribute("data-theme");
    const prefersDark = window.matchMedia?.("(prefers-color-scheme: dark)").matches;
    const next = now ? (now === "dark" ? "light" : "dark") : (prefersDark ? "light" : "dark");
    root.setAttribute("data-theme", next);
    try { localStorage.setItem("monitor-theme", next); } catch { /* ignore */ }
    MonitorMap.setTheme?.(next);
    updateControl();
  });
}

function wireFilters() {
  $("filter-stage").addEventListener("change", (e) => {
    state.filters.stage = e.target.value;
    paintFeed();
  });
  $("filter-show-silenced").addEventListener("change", (e) => {
    state.filters.showSilenced = e.target.checked;
    paintFeed();
  });
}

/* ── boot ────────────────────────────────────────────────────────────────── */
async function boot() {
  wireTabs();
  wireTheme();
  wireFilters();

  try {
    state.config = await API.config();
  } catch {
    state.config = { maps_key: null };
    Panels.toast("Running without console configuration.", "review");
  }

  try {
    const h = await API.health();
    const dot = $("health-dot");
    const healthy = h?.ok !== false && h?.status !== "error";
    dot.dataset.state = healthy ? "up" : "down";
    dot.title = healthy ? "Backend healthy" : "Backend unreachable";
  } catch {
    $("health-dot").dataset.state = "down";
    $("health-dot").title = "Backend unreachable";
  }

  await MonitorMap.init({
    el: $("map"),
    fallbackEl: $("map-fallback"),
    config: state.config,
  });

  await loadWatch();

  // Open a complete read-only pairing so the console is useful immediately.
  if (state.events.length) await selectEvent(state.events[0].canonical_id || state.events[0].event_id);
  if (state.sites.length) await selectSite(state.sites[0].id);
}

boot().catch((err) => {
  console.error("[app] boot failed", err);
  const dot = $("health-dot");
  if (dot) {
    dot.dataset.state = "down";
    dot.title = "Console failed to start";
  }
  Panels.renderDecisionEmpty("The console could not start. Reload the page or check the browser console.");
});
