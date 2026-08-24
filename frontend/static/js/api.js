/* ============================================================================
   API layer — the only module that talks to the network.
   Everything is proxied through this frontend server under /api/*, so the
   browser never needs the backend's origin and there is no CORS story.

   Owned by the shell. map.js / panels.js import from here; they must not
   call fetch() themselves.
   ========================================================================= */

/** Thrown for any non-2xx response. `.status` and `.payload` carry detail. */
export class ApiError extends Error {
  constructor(message, status, payload) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.payload = payload;
  }
}

async function req(path, options = {}) {
  let res;
  try {
    const headers = { accept: "application/json", ...(options.headers || {}) };
    if (options.body !== undefined && !Object.keys(headers).some((key) => key.toLowerCase() === "content-type")) {
      headers["content-type"] = "application/json";
    }
    res = await fetch(path, {
      ...options,
      headers,
    });
  } catch (cause) {
    throw new ApiError(`Cannot reach the backend (${path}).`, 0, { cause: String(cause) });
  }

  const text = await res.text();
  let payload = null;
  if (text) {
    try { payload = JSON.parse(text); } catch { payload = { raw: text }; }
  }

  if (!res.ok) {
    const detail =
      (payload && (payload.detail?.message || payload.detail?.error || payload.detail)) ||
      (payload && payload.message) ||
      res.statusText;
    throw new ApiError(typeof detail === "string" ? detail : `Request failed (${res.status})`, res.status, payload);
  }
  return payload;
}

const qs = (params) => {
  const clean = Object.entries(params || {}).filter(([, v]) => v !== undefined && v !== null && v !== "");
  return clean.length ? "?" + new URLSearchParams(clean).toString() : "";
};

export const API = {
  /* ── console configuration (maps key, map ids, backend base) ───────────── */
  config:        ()                 => req("/api/config"),
  health:        ()                 => req("/api/health"),

  /* ── Phase 1: the public record ───────────────────────────────────────── */
  events:        (filters)          => req("/api/events" + qs(filters)),
  event:         (id)               => req(`/api/events/${encodeURIComponent(id)}`),

  /* ── Phase 2: sites and physical facts ────────────────────────────────── */
  sites:         ()                 => req("/api/sites"),
  site:          (id)               => req(`/api/sites/${encodeURIComponent(id)}`),
  createSite:    (body)             => req("/api/sites", { method: "POST", body: JSON.stringify(body) }),
  bundle:        (id, name)         => req(`/api/sites/${encodeURIComponent(id)}/bundle/${encodeURIComponent(name)}`),
  vicinity:      (id)               => req(`/api/sites/${encodeURIComponent(id)}/vicinity`),
  derived:       (id, metric)       => req(`/api/sites/${encodeURIComponent(id)}/derived/${encodeURIComponent(metric)}`),

  /* ── Phase 3: decisions ───────────────────────────────────────────────── */
  decisions:     (filters)          => req("/api/decisions" + qs(filters)),
  decide:        (body)             => req("/api/decide", { method: "POST", body: JSON.stringify(body) }),

  /* ── evidence / scorecard ─────────────────────────────────────────────── */
  fetchLog:      (filters)          => req("/api/fetch-log" + qs(filters)),
  budget:        ()                 => req("/api/budget"),
  replayRuns:    ()                 => req("/api/replay/runs"),
};

/* ── shared formatting helpers ─────────────────────────────────────────────
   Kept here so the map and the panels label the same value identically.
   ------------------------------------------------------------------------ */

/** ALERT | REVIEW | SILENCE -> lowercase css/token key. Anything else: review. */
export function decisionKey(decision) {
  const d = String(decision || "").toLowerCase();
  if (d === "alert") return "alert";
  if (d === "silence" || d === "quiet") return "silence";
  return "review";
}

export function fmtMetres(m) {
  if (m === null || m === undefined || Number.isNaN(Number(m))) return "—";
  const n = Number(m);
  return n >= 1000 ? `${(n / 1000).toFixed(2)} km` : `${Math.round(n)} m`;
}

export function fmtScore(s) {
  return s === null || s === undefined || Number.isNaN(Number(s)) ? "—" : Number(s).toFixed(2);
}

export function fmtDate(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime())
    ? String(iso).slice(0, 10)
    : d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

/** Mireye tri-state -> a label the UI must never collapse. See Phase 2 D7. */
export function statusLabel(status) {
  switch (String(status || "").toLowerCase()) {
    case "ok":      return { text: "ok",       tone: "ok",       title: "a real value" };
    case "absent":  return { text: "absent",   tone: "absent",   title: "the source answered “nothing here” — a real answer, not a gap" };
    case "failed":  return { text: "withheld", tone: "failed",   title: "the fetch errored — we do not know, and this is not “nothing here”" };
    default:        return { text: "unknown",  tone: "unknown",  title: "no status reported" };
  }
}
