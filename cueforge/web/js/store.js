// CueForge state store.
//
// Holds the latest server snapshot ({ show, runtime }), exposes get() plus a
// subscribe() mechanism, and derives the view helpers the Playing UI needs.
// The server is authoritative; this store never mutates state itself.

let snapshot = { show: null, runtime: null };
const listeners = new Set();

// Derived lookup caches, rebuilt on each set().
let placementById = new Map();          // placementId -> placement
let placementByCell = new Map();        // "page|column|row" -> placement

function cellKey(page, column, row) {
  return page + "|" + column + "|" + row;
}

function rebuildLookups() {
  placementById = new Map();
  placementByCell = new Map();
  const show = snapshot.show;
  if (!show || !Array.isArray(show.placements)) return;
  for (const p of show.placements) {
    placementById.set(p.id, p);
    placementByCell.set(cellKey(p.page, p.column, p.row), p);
  }
}

// Replace the snapshot from a { type:"state", show, runtime } frame.
export function set(msg) {
  snapshot = {
    show: msg && msg.show ? msg.show : null,
    runtime: msg && msg.runtime ? msg.runtime : null,
  };
  rebuildLookups();
  for (const cb of listeners) {
    try { cb(snapshot); } catch (e) { console.error("[store] listener", e); }
  }
}

export function get() {
  return snapshot;
}

export function subscribe(cb) {
  listeners.add(cb);
  return () => listeners.delete(cb);
}

// -------- derived helpers --------

export function show() { return snapshot.show; }
export function runtime() { return snapshot.runtime; }

// Library item for a placement (or by id). Returns null if unresolved.
export function libraryItem(libraryItemId) {
  const s = snapshot.show;
  if (!s || !s.library) return null;
  return s.library[libraryItemId] || null;
}

// Placement lookups.
export function placement(placementId) {
  return placementById.get(placementId) || null;
}

export function placementAt(page, column, row) {
  return placementByCell.get(cellKey(page, column, row)) || null;
}

// Grid cell {page, column, row} for a placement id (placementCell).
export function placementCell(placementId) {
  const p = placementById.get(placementId);
  if (!p) return null;
  return { page: p.page, column: p.column, row: p.row };
}

// The page object for the current runtime page.
export function currentPageObj() {
  const s = snapshot.show, rt = snapshot.runtime;
  if (!s || !rt || !Array.isArray(s.pages)) return null;
  return s.pages.find((pg) => pg.id === rt.currentPage) || null;
}

// Set of played placement ids = sequence[0:cursorIndex] minus the playing one.
export function playedSet() {
  const rt = snapshot.runtime;
  const out = new Set();
  if (!rt || !Array.isArray(rt.sequence)) return out;
  const end = Math.max(0, Math.min(rt.cursorIndex | 0, rt.sequence.length));
  for (let i = 0; i < end; i++) out.add(rt.sequence[i]);
  if (rt.playing && rt.playing.placementId) out.delete(rt.playing.placementId);
  return out;
}

// The standby (cursor) placement id = sequence[cursorIndex], or null when
// parked at the end of the sequence.
export function standbyPlacementId() {
  const rt = snapshot.runtime;
  if (!rt || !Array.isArray(rt.sequence)) return null;
  const i = rt.cursorIndex | 0;
  if (i < 0 || i >= rt.sequence.length) return null;
  return rt.sequence[i];
}

// Map of placementId -> background status entry, for quick cell lookups.
export function backgroundsById() {
  const rt = snapshot.runtime;
  const m = new Map();
  if (rt && Array.isArray(rt.backgrounds)) {
    for (const b of rt.backgrounds) m.set(b.placementId, b);
  }
  return m;
}

// True while the engine's show voices are frozen (pause/resume transport).
export function paused() {
  return !!(snapshot.runtime && snapshot.runtime.paused);
}

// Map of placementId -> {remainingMs, kind} for pending chain fires ("armed"
// cells): placements a chain has scheduled but not yet activated.
export function scheduledById() {
  const rt = snapshot.runtime;
  const m = new Map();
  if (rt && Array.isArray(rt.scheduled)) {
    for (const s of rt.scheduled) m.set(s.placementId, { remainingMs: s.remainingMs, kind: s.kind });
  }
  return m;
}

// The show's defined named audio Outputs (settings.outputs): [{id, name,
// device, channel, mono}]. Edited via the "setOutputs" WS action. The Default
// Output (device None, channels 1-2) is implicit and never appears here.
export function outputs() {
  const s = snapshot.show;
  return (s && s.settings && s.settings.outputs) || [];
}

// Map of outputId -> {id, deviceOk, deviceChannels}, the runtime availability
// mirror of outputs() (see runtime.outputs in PROTOCOL.md).
export function outputAvailability() {
  const rt = snapshot.runtime;
  const m = new Map();
  if (rt && Array.isArray(rt.outputs)) {
    for (const o of rt.outputs) m.set(o.id, o);
  }
  return m;
}

// Convenience label for an outputId (null/dangling -> "Default").
export function outputLabel(id) {
  if (!id) return "Default";
  const o = outputs().find((x) => x.id === id);
  return o ? o.name : "Default";
}
