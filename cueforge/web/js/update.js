// CueForge application self-update client. Fetches /api/update/status (the
// server checks GitHub Releases in the background), drives the little
// "update available" dot on the Settings tab / hamburger, and provides the
// check/apply calls used by the Settings "Application" card.

import { authHeaders } from "./ws.js";

let statusCache = null;

async function requestStatus(url, opts) {
  const res = await fetch(url, Object.assign({}, opts, {
    headers: authHeaders(opts && opts.headers),
  }));
  if (!res.ok) {
    let detail = "";
    try { detail = (await res.json()).detail || ""; } catch { /* ignore */ }
    throw new Error(detail || `HTTP ${res.status}`);
  }
  return res.json();
}

export function getUpdateStatus() {
  return statusCache;
}

/** Refresh the cached status from the server and re-render the badge.
 *  Quiet on failure (offline / not authed yet) -- keeps the last value. */
export async function refreshUpdateStatus() {
  try {
    statusCache = await requestStatus("/api/update/status");
  } catch {
    return statusCache;
  }
  renderBadge();
  return statusCache;
}

/** Explicit "Check now": forces a server-side GitHub query. Throws on error. */
export async function forceUpdateCheck() {
  statusCache = await requestStatus("/api/update/check", { method: "POST" });
  renderBadge();
  return statusCache;
}

/** Start the server-side download + swap + restart. Throws on error. */
export function applyUpdate() {
  return requestStatus("/api/update/apply", { method: "POST" });
}

function renderBadge() {
  const on = !!(statusCache && statusCache.updateAvailable);
  document.querySelectorAll("[data-update-badge]").forEach((el) => {
    el.hidden = !on;
  });
}
