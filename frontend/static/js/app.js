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
  vicinityCache: new Map(),
  derivedCache: new Map(),
};

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
  Panels.renderFeed(state.events, {
    onSelect: selectEvent,
    selectedId: state.selectedEventId,
    filters: state.filters,
    decisions: state.decisions,
  });
  $("feed-count").textContent = String(state.events.length);
  $("feed-empty").hidden = state.events.length > 0;
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
  state.selectedEventId = eventId;
  paintFeed();
  await loadDecisions();
  paintSites();
  if (state.selectedSiteId) await showDecision();
}

async function selectSite(siteId) {
  state.selectedSiteId = siteId;
  paintSites();

  const site = currentSite();
  if (!site) return;

  let vicinity = state.vicinityCache.get(siteId);
  if (vicinity === undefined) {
    try {
      vicinity = await API.vicinity(siteId);
    } catch (err) {
      // A site with no ring scan is normal, not an error worth shouting about.
      vicinity = err instanceof ApiError && err.status === 404 ? null : null;
    }
    state.vicinityCache.set(siteId, vicinity);
  }

  await MonitorMap.focusSite(site, { vicinity, decision: decisionForSite(siteId) });
  await showDecision();
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

async function showDecision() {
  const site = currentSite();
  const event = currentEvent();
  if (!site || !event) {
    Panels.renderDecisionEmpty("Select an event and a site to see the decision.");
    return;
  }

  let decision = decisionForSite(site.id);
  if (!decision) {
    try {
      decision = await API.decide({ event, site_id: site.id });
      state.decisions.push(decision);
    } catch (err) {
      Panels.renderDecisionEmpty(
        err instanceof ApiError ? `Could not evaluate: ${err.message}` : "Could not evaluate this pairing."
      );
      return;
    }
  }

  let derived = null;
  if (decision.metric) {
    const key = `${site.id}:${decision.metric}`;
    derived = state.derivedCache.get(key);
    if (derived === undefined) {
      try { derived = await API.derived(site.id, decision.metric); }
      catch { derived = null; }
      state.derivedCache.set(key, derived);
    }
  }

  Panels.renderDecision(decision, {
    event,
    site,
    derived,
    vicinity: state.vicinityCache.get(site.id) || null,
  });
  MonitorMap.setDecision(site.id, decisionKey(decision.decision));
  paintSites();
}

/* ── loaders ─────────────────────────────────────────────────────────────── */
async function loadDecisions() {
  try {
    const res = await API.decisions(
      state.selectedEventId ? { canonical_id: state.selectedEventId } : {}
    );
    state.decisions = Array.isArray(res) ? res : res?.decisions || [];
  } catch {
    state.decisions = [];
  }
}

async function loadWatch() {
  const [events, sites] = await Promise.allSettled([API.events(), API.sites()]);
  state.events = events.status === "fulfilled" ? (events.value || []) : [];
  state.sites  = sites.status  === "fulfilled" ? (sites.value  || []) : [];

  if (events.status === "rejected") Panels.toast("Could not load the public record.", "alert");
  if (sites.status === "rejected")  Panels.toast("Could not load monitored sites.", "alert");

  await loadDecisions();
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

function wireTabs() {
  for (const [tabId, viewId, loader] of VIEWS) {
    $(tabId).addEventListener("click", async () => {
      for (const [t, v] of VIEWS) {
        const active = t === tabId;
        $(t).classList.toggle("is-active", active);
        $(t).setAttribute("aria-selected", String(active));
        $(v).hidden = !active;
        $(v).classList.toggle("is-active", active);
      }
      if (loader) await loader();
    });
  }
}

/* ── theme ───────────────────────────────────────────────────────────────── */
function wireTheme() {
  const root = document.documentElement;
  let stored = null;
  try { stored = localStorage.getItem("monitor-theme"); } catch { /* private mode */ }
  if (stored === "dark" || stored === "light") root.setAttribute("data-theme", stored);

  $("theme-toggle").addEventListener("click", () => {
    const now = root.getAttribute("data-theme");
    const prefersDark = window.matchMedia?.("(prefers-color-scheme: dark)").matches;
    const next = now ? (now === "dark" ? "light" : "dark") : (prefersDark ? "light" : "dark");
    root.setAttribute("data-theme", next);
    try { localStorage.setItem("monitor-theme", next); } catch { /* ignore */ }
    MonitorMap.setTheme?.(next);
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
    dot.dataset.state = h?.ok === false ? "down" : "up";
    dot.title = h?.ok === false ? "Backend unreachable" : "Backend healthy";
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

  // Open on the first event so the console is never a blank screen.
  if (state.events.length) await selectEvent(state.events[0].canonical_id || state.events[0].event_id);
}

boot();
