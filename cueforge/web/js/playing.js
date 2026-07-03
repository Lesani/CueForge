// CueForge Playing section: renders the page-tab row, cue grid, cursor, and the
// bottom control bar; wires firing / long-press standby and the GO/PANIC/Reset
// controls. Reads state from the store; sends actions over the ws module.

import * as store from "./store.js";
import { send, isConnected } from "./ws.js";
import {
  formatTimer, formatClock, framesToSeconds, itemDuration, esc,
} from "./util.js";
import { importFileWithDedup } from "./importer.js";
import { confirmDialog, promptDialog } from "./dialogs.js";

const LOOP_ICON = "↻";   // ↻
const STOP_ICON = "⊘";   // ⊘
const CHEV = "∨∨";  // ∨∨
const PLAY_TRI = "▶";    // ▶
const NEXT_TRI = "▸";    // ▸
const CHECK = "✓";       // ✓

// Marker prefix for our own internal drag payload (native HTML5 DnD), so a
// dropped placement can be told apart from an OS file drop on the same
// "text/plain" data slot.
const DRAG_MARKER = "cueforge-placement:";

let els = null;               // cached DOM references
let unsub = null;

// Live-timer smoothing state, captured from each snapshot.
let sync = {
  pid: null,        // currently playing placement id (null = idle)
  total: 0,         // total seconds of the playing cue
  remainAt: 0,      // remaining seconds at capture time
  at: 0,            // performance.now() of capture
};
let goLockUntil = 0;          // performance.now() until which GO stays disabled

// Long-press / tap tracking (pointer delegation on the grid).
let press = { pid: null, timer: null, longFired: false, x: 0, y: 0 };
let lastSig = null;
let lastPageTabsSig = null;
let lastNp = { name: "", next: "" };

// Background-cue running-time cells: placementId -> .meta element, rebuilt on
// every renderGrid so the rAF/snapshot path can tick them without re-rendering.
let bgMetaEls = new Map();
let bgMetaText = new Map();   // last text written, to skip no-op writes

// Auto-scroll the standby cursor into view only when its position actually
// changed (page + standby placement id), not on unrelated re-renders.
let lastCursorKey = null;

// Touch / pen cue-move state (coexists with native mouse HTML5 DnD in edit
// mode). "mouse" pointers are left to the native dragstart/drop path.
let tmove = {
  active: false,     // move mode engaged (past the hold timer)
  pid: null,         // placement being moved
  pointerId: null,
  holdTimer: null,
  startX: 0, startY: 0,
  srcCell: null,     // source .cell element
  ghost: null,       // floating label following the finger
  target: null,      // currently highlighted [data-col] drop target
};

// ---------------------------------------------------------------- mount

export function mount(sectionEl) {
  sectionEl.innerHTML = `
    <div class="banner device" data-device hidden>Audio device unavailable</div>
    <div class="banner loading" data-loading hidden></div>
    <div class="pagerow">
      <div class="pagetabs" data-pagetabs></div>
      <div class="pageactions">
        <button class="btn" data-addcol type="button" hidden>+ Add column</button>
        <button class="btn" data-edit type="button">Edit</button>
        <button class="btn" data-reset type="button">Reset</button>
      </div>
    </div>
    <div class="grid" data-grid></div>
    <div class="controlbar">
      <div class="livestatus">
        <span class="timer" data-timer> 00:00</span>
        <div class="np-text">
          <div class="np-label">Now playing</div>
          <div class="np-name" data-npname>Idle</div>
          <div class="np-next" data-npnext></div>
        </div>
      </div>
      <button class="go" data-go type="button">GO</button>
      <div class="panic-wrap">
        <button class="panic" data-panic type="button">PANIC</button>
      </div>
    </div>`;

  els = {
    device: sectionEl.querySelector("[data-device]"),
    loading: sectionEl.querySelector("[data-loading]"),
    pagetabs: sectionEl.querySelector("[data-pagetabs]"),
    edit: sectionEl.querySelector("[data-edit]"),
    addcol: sectionEl.querySelector("[data-addcol]"),
    reset: sectionEl.querySelector("[data-reset]"),
    grid: sectionEl.querySelector("[data-grid]"),
    timer: sectionEl.querySelector("[data-timer]"),
    npname: sectionEl.querySelector("[data-npname]"),
    npnext: sectionEl.querySelector("[data-npnext]"),
    go: sectionEl.querySelector("[data-go]"),
    panic: sectionEl.querySelector("[data-panic]"),
    // per-frame targets, refreshed after each grid render:
    fill: null,
    live: null,
  };

  wireControls();
  wireGridInput();

  unsub = store.subscribe(onSnapshot);
  onSnapshot(store.get());
  requestAnimationFrame(frame);
}

// ---------------------------------------------------------------- controls

function wireControls() {
  els.go.addEventListener("click", () => send("go"));
  els.panic.addEventListener("click", () => send("panic"));
  els.reset.addEventListener("click", async () => {
    if (await confirmDialog("Reset all pages to the top and stop everything?", { danger: true })) {
      send("reset");
    }
  });
  els.edit.addEventListener("click", () => {
    const rt = store.runtime();
    const on = !(rt && rt.editMode);
    send("setEditMode", { on });
  });
  els.addcol.addEventListener("click", async () => {
    const page = store.currentPageObj();
    if (!page) return;
    const name = await promptDialog("New column name:", { initial: "Act " + ((page.columns || []).length + 1) });
    if (name && name.trim()) send("addColumn", { page: page.id, name: name.trim() });
  });
}

function wireGridInput() {
  const grid = els.grid;

  grid.addEventListener("pointerdown", (e) => {
    const cell = e.target.closest(".cell[data-pid]");
    if (!cell) return;
    const rt = store.runtime();
    if (rt && rt.editMode) return;      // edit interactions handled elsewhere
    press.pid = cell.dataset.pid;
    press.longFired = false;
    press.x = e.clientX;
    press.y = e.clientY;
    clearTimeout(press.timer);
    press.timer = setTimeout(() => {
      press.longFired = true;
      send("standby", { placementId: press.pid });  // silent
      flashStandby(press.pid);
    }, 500);
  });

  grid.addEventListener("pointermove", (e) => {
    if (press.pid == null) return;
    if (Math.abs(e.clientX - press.x) > 10 || Math.abs(e.clientY - press.y) > 10) {
      clearTimeout(press.timer);        // treat as scroll/drag: cancel press
      press.pid = null;
    }
  });

  const endPress = (e) => {
    if (press.pid == null) return;
    clearTimeout(press.timer);
    const cell = e.target.closest ? e.target.closest(".cell[data-pid]") : null;
    if (!press.longFired && cell && cell.dataset.pid === press.pid) {
      send("fire", { placementId: press.pid });
    }
    press.pid = null;
  };
  grid.addEventListener("pointerup", endPress);
  grid.addEventListener("pointercancel", () => {
    clearTimeout(press.timer);
    press.pid = null;
  });

  // Touch / pen cue-move (edit mode). Mouse keeps native HTML5 DnD.
  grid.addEventListener("pointerdown", onTouchMoveDown);
  grid.addEventListener("pointermove", onTouchMoveMove);
  grid.addEventListener("pointerup", onTouchMoveUp);
  grid.addEventListener("pointercancel", onTouchMoveCancel);

  wireEditModeDnd(grid);
}

// ---------------------------------------------------------------- touch move

function onTouchMoveDown(e) {
  if (e.pointerType === "mouse") return;   // native DnD handles mouse
  const rt = store.runtime();
  if (!rt || !rt.editMode) return;
  // Do not hijack taps on the remove "x", column inputs, or header buttons.
  if (e.target.closest("[data-remove], input, button")) return;
  const cell = e.target.closest(".cell[data-pid]");
  if (!cell) return;
  tmove.pid = cell.dataset.pid;
  tmove.srcCell = cell;
  tmove.pointerId = e.pointerId;
  tmove.startX = e.clientX;
  tmove.startY = e.clientY;
  tmove.active = false;
  clearTimeout(tmove.holdTimer);
  tmove.holdTimer = setTimeout(() => beginTouchMove(e.clientX, e.clientY), 350);
}

function beginTouchMove(x, y) {
  if (tmove.pid == null || !tmove.srcCell) return;
  tmove.active = true;
  try { els.grid.setPointerCapture(tmove.pointerId); } catch { /* ignore */ }
  els.grid.style.touchAction = "none";      // stop the grid scrolling mid-move
  tmove.srcCell.classList.add("touch-dragging");

  const labelEl = tmove.srcCell.querySelector(".name .label");
  const ghost = document.createElement("div");
  ghost.className = "touch-ghost";
  ghost.textContent = labelEl ? labelEl.textContent : "Cue";
  document.body.appendChild(ghost);
  tmove.ghost = ghost;
  positionGhost(x, y);
  try { navigator.vibrate && navigator.vibrate(20); } catch { /* ignore */ }
}

function positionGhost(x, y) {
  if (!tmove.ghost) return;
  tmove.ghost.style.left = x + "px";
  tmove.ghost.style.top = y + "px";
}

function clearTouchTarget() {
  if (tmove.target) {
    tmove.target.classList.remove("drop-target");
    tmove.target = null;
  }
}

function onTouchMoveMove(e) {
  if (tmove.pid == null || e.pointerId !== tmove.pointerId) return;
  if (!tmove.active) {
    // Still within the hold window: a real move means the operator is
    // scrolling, so cancel the pending move.
    if (Math.abs(e.clientX - tmove.startX) > 8 ||
        Math.abs(e.clientY - tmove.startY) > 8) {
      resetTouchMove();
    }
    return;
  }
  e.preventDefault();
  positionGhost(e.clientX, e.clientY);
  const under = document.elementFromPoint(e.clientX, e.clientY);
  const cell = under ? under.closest("[data-col]") : null;
  if (cell !== tmove.target) {
    clearTouchTarget();
    if (cell) {
      cell.classList.add("drop-target");
      tmove.target = cell;
    }
  }
}

function onTouchMoveUp(e) {
  if (tmove.pid == null || e.pointerId !== tmove.pointerId) return;
  if (!tmove.active) { resetTouchMove(); return; }
  const under = document.elementFromPoint(e.clientX, e.clientY);
  const cell = under ? under.closest("[data-col][data-row]") : null;
  if (cell) {
    const page = store.currentPageObj();
    if (page) {
      send("moveCue", {
        placementId: tmove.pid,
        toColumn: cell.dataset.col,
        toRow: parseInt(cell.dataset.row, 10),
      });
    }
  }
  resetTouchMove();
}

function onTouchMoveCancel(e) {
  if (tmove.pid == null || e.pointerId !== tmove.pointerId) return;
  resetTouchMove();
}

function resetTouchMove() {
  clearTimeout(tmove.holdTimer);
  clearTouchTarget();
  if (tmove.ghost) { tmove.ghost.remove(); tmove.ghost = null; }
  if (tmove.srcCell) tmove.srcCell.classList.remove("touch-dragging");
  if (tmove.active && tmove.pointerId != null) {
    try { els.grid.releasePointerCapture(tmove.pointerId); } catch { /* ignore */ }
  }
  els.grid.style.touchAction = "";
  tmove.active = false;
  tmove.pid = null;
  tmove.srcCell = null;
  tmove.pointerId = null;
  tmove.target = null;
}

// Brief visual + haptic confirmation that a long-press landed the standby
// cursor (the server round-trip that actually moves the cursor lags behind).
function flashStandby(pid) {
  const selector = window.CSS && CSS.escape
    ? '.cell[data-pid="' + CSS.escape(pid) + '"]'
    : '.cell[data-pid="' + pid + '"]';
  const cell = els.grid.querySelector(selector);
  if (cell) {
    cell.classList.add("standby-flash");
    setTimeout(() => { if (cell.isConnected) cell.classList.remove("standby-flash"); }, 350);
  }
  try { navigator.vibrate && navigator.vibrate(30); } catch { /* ignore */ }
}

// ---------------------------------------------------------------- edit-mode DnD

let lastDropTarget = null;

function clearDropTarget() {
  if (lastDropTarget) {
    lastDropTarget.classList.remove("drop-target");
    lastDropTarget = null;
  }
}

function wireEditModeDnd(grid) {
  // Click delegation: remove-placement "x", empty-cell -> library picker.
  grid.addEventListener("click", (e) => {
    const rt = store.runtime();
    if (!rt || !rt.editMode) return;

    const removeBtn = e.target.closest("[data-remove]");
    if (removeBtn) {
      e.stopPropagation();
      send("removePlacement", { placementId: removeBtn.dataset.remove });
      return;
    }

    const emptyCell = e.target.closest(".cell.empty.editable");
    if (emptyCell) {
      const page = store.currentPageObj();
      if (!page) return;
      openLibraryPicker({
        page: page.id,
        column: emptyCell.dataset.col,
        row: parseInt(emptyCell.dataset.row, 10),
      });
    }
  });

  // Drag a placed cell onto another cell -> moveCue (server swaps if occupied).
  grid.addEventListener("dragstart", (e) => {
    const rt = store.runtime();
    if (!rt || !rt.editMode) return;
    const cell = e.target.closest(".cell[data-pid]");
    if (!cell) return;
    e.dataTransfer.setData("text/plain", DRAG_MARKER + cell.dataset.pid);
    e.dataTransfer.effectAllowed = "move";
  });

  grid.addEventListener("dragover", (e) => {
    const rt = store.runtime();
    if (!rt || !rt.editMode) return;
    const cell = e.target.closest("[data-col]");
    if (!cell) return;
    e.preventDefault();
    const isFiles = e.dataTransfer.types && Array.from(e.dataTransfer.types).includes("Files");
    e.dataTransfer.dropEffect = isFiles ? "copy" : "move";
    if (lastDropTarget !== cell) {
      clearDropTarget();
      cell.classList.add("drop-target");
      lastDropTarget = cell;
    }
  });

  grid.addEventListener("dragleave", (e) => {
    const cell = e.target.closest("[data-col]");
    if (cell && cell === lastDropTarget) {
      if (!e.relatedTarget || !cell.contains(e.relatedTarget)) clearDropTarget();
    }
  });

  grid.addEventListener("drop", (e) => {
    const rt = store.runtime();
    clearDropTarget();
    if (!rt || !rt.editMode) return;
    const cell = e.target.closest("[data-col]");
    if (!cell) return;
    e.preventDefault();

    const page = store.currentPageObj();
    if (!page) return;
    const col = cell.dataset.col;
    const row = parseInt(cell.dataset.row, 10);

    const files = e.dataTransfer.files;
    if (files && files.length) {
      handleFileDrop(page, col, row, Array.from(files));
      return;
    }
    const raw = e.dataTransfer.getData("text/plain");
    if (raw && raw.indexOf(DRAG_MARKER) === 0) {
      const placementId = raw.slice(DRAG_MARKER.length);
      send("moveCue", { placementId, toColumn: col, toRow: row });
    }
  });
}

// Walk column-major from (startColId, startRow) across the rest of the page,
// collecting up to `count` empty cell addresses (skips occupied cells,
// including the drop target itself if it is occupied).
function findEmptyCellsFrom(page, startColId, startRow, count) {
  const cols = page.columns || [];
  const startIdx = cols.findIndex((c) => c.id === startColId);
  if (startIdx < 0) return [];
  const out = [];
  for (let ci = startIdx; ci < cols.length && out.length < count; ci++) {
    const col = cols[ci];
    const rows = Math.max(0, col.rows | 0);
    const rowStart = ci === startIdx ? startRow : 0;
    for (let r = rowStart; r < rows && out.length < count; r++) {
      if (!store.placementAt(page.id, col.id, r)) {
        out.push({ page: page.id, column: col.id, row: r });
      }
    }
  }
  return out;
}

// Multi-file OS drop: import each file (dedup-modal per duplicate) and place
// it into the next empty cell, column-major from the drop target.
async function handleFileDrop(page, col, row, files) {
  const targets = findEmptyCellsFrom(page, col, row, files.length);
  if (targets.length < files.length) {
    console.warn("[playing] not enough empty cells for all dropped files;", targets.length, "of", files.length, "will be placed");
  }
  for (let i = 0; i < targets.length; i++) {
    await importFileWithDedup(files[i], targets[i]);
  }
}

// ---------------------------------------------------------------- library picker

// One picker row: name (ellipsized) + type badge + right-aligned duration,
// matching the visual language of the library tab's own rows.
function pickerRowHTML(it) {
  const dur = it.type === "stop"
    ? "STOP"
    : (itemDuration(it) != null ? formatClock(itemDuration(it)) : "--:--");
  return `
    <button type="button" class="picker-item" data-pick="${esc(it.id)}">
      <span class="name">${esc(it.name)}</span>
      <span class="type-badge ${esc(it.type)}">${esc(it.type)}</span>
      <span class="duration">${esc(dur)}</span>
    </button>`;
}

// Builds the picker list body: flat when no item carries a group, grouped
// under group headers (ungrouped items last) otherwise. `items` is
// already name-sorted; grouping preserves that order within each group.
function pickerListHTML(items, filterText) {
  if (!items.length) {
    return `<div class="picker-empty">No library items yet. Import audio from the Library tab, or drag a file onto this cell.</div>`;
  }
  const q = filterText.trim().toLowerCase();
  const filtered = q ? items.filter((it) => it.name.toLowerCase().includes(q)) : items;
  if (!filtered.length) {
    return `<div class="picker-empty">No items match.</div>`;
  }

  const hasGroups = items.some((it) => it.group);
  if (!hasGroups) {
    return filtered.map(pickerRowHTML).join("");
  }

  const groups = new Map();
  for (const it of filtered) {
    const key = it.group || "";
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(it);
  }
  const groupNames = Array.from(groups.keys())
    .filter((k) => k !== "")
    .sort((a, b) => a.localeCompare(b));

  let html = "";
  for (const name of groupNames) {
    html += `<div class="picker-group-head">${esc(name)}</div>`;
    html += groups.get(name).map(pickerRowHTML).join("");
  }
  if (groups.has("")) {
    html += `<div class="picker-group-head">No group</div>`;
    html += groups.get("").map(pickerRowHTML).join("");
  }
  return html;
}

function openLibraryPicker(target) {
  const show = store.show();
  const items = show && show.library
    ? Object.values(show.library).sort((a, b) => a.name.localeCompare(b.name))
    : [];

  const overlay = document.createElement("div");
  overlay.className = "modal-overlay";
  overlay.innerHTML = `
    <div class="modal picker-modal">
      <div class="picker-header">
        <h2>Place a cue</h2>
        <input type="text" class="dialog-input picker-search" data-search placeholder="Search..." />
      </div>
      <div class="picker-list" data-picker-list></div>
      <div class="picker-footer">
        <button type="button" class="btn picker-newstop" data-newstop>+ New stop cue here</button>
      </div>
      <div class="modal-actions">
        <button class="btn" type="button" data-cancel>Cancel</button>
      </div>
    </div>`;
  document.body.appendChild(overlay);

  function close() {
    overlay.remove();
    document.removeEventListener("keydown", onKey);
  }
  function onKey(e) {
    if (e.key === "Escape") close();
  }
  document.addEventListener("keydown", onKey);

  const list = overlay.querySelector("[data-picker-list]");
  const searchInput = overlay.querySelector("[data-search]");

  function renderList() {
    list.innerHTML = pickerListHTML(items, searchInput.value);
    list.querySelectorAll("[data-pick]").forEach((btn) => {
      btn.addEventListener("click", () => {
        send("placeCue", {
          libraryItemId: btn.dataset.pick,
          page: target.page, column: target.column, row: target.row,
        });
        close();
      });
    });
  }
  renderList();
  searchInput.addEventListener("input", renderList);

  overlay.querySelector("[data-newstop]").addEventListener("click", () => {
    send("createStopCue", { page: target.page, column: target.column, row: target.row });
    close();
  });
  overlay.querySelector("[data-cancel]").addEventListener("click", close);
  overlay.addEventListener("click", (e) => { if (e.target === overlay) close(); });

  // Coarse pointers (touch/iPad): don't pop the keyboard on open. Fine
  // pointers (mouse/trackpad): autofocus so typing starts filtering at once.
  if (window.matchMedia && window.matchMedia("(pointer: fine)").matches) {
    searchInput.focus();
  }
}

// ---------------------------------------------------------------- snapshot

function structuralSig(s) {
  const show = s.show, rt = s.runtime;
  if (!show || !rt) return "empty";
  const bg = (rt.backgrounds || []).map((b) => b.placementId).sort().join(",");
  // Library revision proxy: names/types/trims affect cell text.
  let lib = "";
  for (const k in show.library) {
    const it = show.library[k];
    lib += k + it.name + it.type + it.loop + it.trimIn + it.trimOut + ";";
  }
  const plc = (show.placements || [])
    .map((p) => p.id + p.column + p.row).join(",");
  const pages = (show.pages || []).map((pg) =>
    pg.id + pg.name + "[" + (pg.columns || []).map((c) => c.id + c.name + c.rows).join(",") + "]"
  ).join("|");
  return [
    rt.currentPage, rt.editMode, rt.cursorIndex,
    rt.playing ? rt.playing.placementId : "",
    bg, plc, lib, pages,
  ].join("|");
}

function onSnapshot(s) {
  if (!els) return;
  const rt = s.runtime;

  // Banners.
  if (rt && rt.deviceOk === false) els.device.hidden = false;
  else els.device.hidden = true;

  if (rt && rt.loading && rt.loading.total > 0 &&
      rt.loading.done < rt.loading.total) {
    els.loading.hidden = false;
    els.loading.textContent =
      `Loading audio ${rt.loading.done}/${rt.loading.total}`;
  } else {
    els.loading.hidden = true;
  }

  // Edit toggle reflects shared state.
  if (rt) {
    els.edit.classList.toggle("active", !!rt.editMode);
    els.addcol.hidden = !rt.editMode;
  }

  // GO lock window.
  const lock = rt ? (rt.goLockRemainingMs | 0) : 0;
  goLockUntil = lock > 0 ? performance.now() + lock : 0;

  // Timer smoothing capture. Playback is deterministic 1.0x real time and the
  // reported frame only advances per audio-callback block, so re-anchoring the
  // wall clock on every 15 Hz tick would quantize the smooth rAF fill to the
  // block boundaries (visible stutter). Anchor once per cue and only re-anchor
  // on a real discontinuity (restart/seek: report far from the local clock).
  if (rt && rt.playing) {
    const p = rt.playing;
    const total = framesToSeconds(p.totalFrames);
    const remain = framesToSeconds(p.totalFrames - p.frame);
    const now = performance.now();
    if (sync.pid !== p.placementId) {
      sync = { pid: p.placementId, total, remainAt: remain, at: now };
    } else {
      const predicted = sync.remainAt - (now - sync.at) / 1000;
      if (Math.abs(predicted - remain) > 0.25) {
        sync.remainAt = remain;
        sync.at = now;
      }
      sync.total = total;
    }
  } else {
    sync.pid = null;
    sync.total = 0;
    sync.remainAt = 0;
  }

  // Only rebuild the page-tab row when it actually changed structurally; it
  // holds inline rename inputs in edit mode that a 15 Hz re-render would
  // otherwise wipe out mid-keystroke.
  const ptSig = pageTabsSig(s);
  if (ptSig !== lastPageTabsSig) {
    lastPageTabsSig = ptSig;
    renderPageTabs(s);
  }

  // Only rebuild the grid on structural/state changes; the RAF loop handles the
  // live fill + countdown so we do not churn the DOM at 15 Hz.
  const sig = structuralSig(s);
  if (sig !== lastSig) {
    lastSig = sig;
    renderGrid(s);
  }

  // Tick background-cue running times without re-rendering the grid. The sig
  // includes bg placement ids, so start/stop already re-renders (and rebuilds
  // the cache); here we only update the whole-second text on existing cells.
  updateBackgroundTimes(rt);

  renderNowPlaying(s);
}

function updateBackgroundTimes(rt) {
  if (!rt || !Array.isArray(rt.backgrounds)) return;
  for (const bg of rt.backgrounds) {
    const el = bgMetaEls.get(bg.placementId);
    if (!el) continue;
    const txt = formatClock(framesToSeconds(bg.frame));
    if (bgMetaText.get(bg.placementId) !== txt) {
      el.textContent = txt;
      bgMetaText.set(bg.placementId, txt);
    }
  }
}

// ---------------------------------------------------------------- page tabs

function pageTabsSig(s) {
  const show = s.show, rt = s.runtime;
  if (!show || !rt) return "empty";
  const pages = (show.pages || []).map((pg) => pg.id + pg.name).join(",");
  return [rt.currentPage, !!rt.editMode, pages].join("|");
}

function renderPageTabs(s) {
  const show = s.show, rt = s.runtime;
  const tabs = els.pagetabs;
  if (!show || !Array.isArray(show.pages)) { tabs.innerHTML = ""; return; }
  const editMode = !!(rt && rt.editMode);

  tabs.innerHTML = show.pages.map((pg) => {
    const active = rt && pg.id === rt.currentPage ? " active" : "";
    const del = editMode
      ? `<span class="tab-del" data-delpage="${esc(pg.id)}" title="Delete page">&times;</span>`
      : "";
    return `<div class="pagetab${active}" data-page="${esc(pg.id)}">`
      + `<span class="pagetab-name" data-pagename="${esc(pg.id)}">${esc(pg.name)}</span>`
      + del
      + `</div>`;
  }).join("");
  if (editMode) {
    tabs.innerHTML += `<button class="btn addpage" type="button" data-addpage>+ Page</button>`;
  }

  tabs.querySelectorAll(".pagetab").forEach((el) => {
    el.addEventListener("click", (e) => {
      if (e.target.closest("[data-delpage]")) return;
      if (e.target.closest(".pagetab-name.editing")) return;
      send("setPage", { pageId: el.dataset.page });
    });
  });

  if (!editMode) return;

  tabs.querySelectorAll("[data-pagename]").forEach((nameEl) => {
    nameEl.addEventListener("dblclick", (e) => {
      e.stopPropagation();
      startRenamePage(nameEl, show);
    });
  });
  tabs.querySelectorAll("[data-delpage]").forEach((btn) => {
    btn.addEventListener("click", async (e) => {
      e.stopPropagation();
      const pageId = btn.dataset.delpage;
      const page = show.pages.find((p) => p.id === pageId);
      const count = (show.placements || []).filter((p) => p.page === pageId).length;
      const name = page ? page.name : "this page";
      if (await confirmDialog(`Delete page "${name}"? It has ${count} cue${count === 1 ? "" : "s"}.`, { danger: true })) {
        send("removePage", { pageId });
      }
    });
  });
  const addBtn = tabs.querySelector("[data-addpage]");
  if (addBtn) {
    addBtn.addEventListener("click", async () => {
      const name = await promptDialog("New page name:", { initial: "Page " + (show.pages.length + 1) });
      if (name && name.trim()) send("addPage", { name: name.trim() });
    });
  }
}

function startRenamePage(nameEl, show) {
  const pageId = nameEl.dataset.pagename;
  const page = show.pages.find((p) => p.id === pageId);
  if (!page) return;
  nameEl.classList.add("editing");
  nameEl.innerHTML = "";
  const input = document.createElement("input");
  input.type = "text";
  input.className = "inline-rename";
  input.value = page.name;
  nameEl.appendChild(input);
  input.focus();
  input.select();
  input.addEventListener("click", (e) => e.stopPropagation());
  const commit = () => {
    const v = input.value.trim();
    if (v && v !== page.name) send("renamePage", { pageId, name: v });
  };
  input.addEventListener("blur", commit);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") input.blur();
    if (e.key === "Escape") { input.value = page.name; input.blur(); }
  });
}

// ---------------------------------------------------------------- grid

function iconHTML(item) {
  if (!item) return "";
  if (item.type === "stop") return `<span class="stopmark">${STOP_ICON}</span>`;
  if (item.type === "background" && item.loop) {
    return `<span class="loop">${LOOP_ICON}</span>`;
  }
  return "";
}

// Cue type identity on the cell itself, so background/stop cues read as such
// BEFORE they are played (running/played states add their own colors on top).
function typeClass(item) {
  if (!item) return "";
  if (item.type === "stop") return " stoptype";
  if (item.type === "background") return " bgtype";
  return "";
}

function typeTagHTML(item) {
  return item && item.type === "background" ? `<span class="tag">bg</span>` : "";
}

function metaText(item) {
  if (!item) return "";
  if (item.type === "stop") return "STOP";
  const d = itemDuration(item);
  return d != null ? formatClock(d) : "--:--";
}

function renderGrid(s) {
  const grid = els.grid;
  const page = store.currentPageObj();
  if (!page || !Array.isArray(page.columns)) {
    grid.innerHTML = "";
    grid.style.gridTemplateColumns = "";
    return;
  }

  const cols = page.columns;
  const rt = s.runtime;
  const editMode = !!(rt && rt.editMode);
  const totalCols = Math.max(1, cols.length);
  grid.style.gridTemplateColumns = `repeat(${totalCols}, minmax(min(300px, 85vw), 1fr))`;

  const playingPid = rt && rt.playing ? rt.playing.placementId : null;
  const showCursor = !playingPid;
  const standbyPid = store.standbyPlacementId();
  const played = store.playedSet();
  const bgById = store.backgroundsById();

  let html = "";
  for (let ci = 0; ci < cols.length; ci++) {
    const col = cols[ci];
    const rows = Math.max(0, col.rows | 0);

    // Count placed cues in this column for the header.
    let count = 0;
    for (let r = 0; r < rows; r++) {
      if (store.placementAt(page.id, col.id, r)) count++;
    }

    html += `<div class="column" data-colwrap="${esc(col.id)}">`;
    if (editMode) {
      html += `<div class="col-head edit">`
        + `<span class="num">${ci + 1}</span>`
        + `<input type="text" class="colname-input" data-colname="${esc(col.id)}" value="${esc(col.name)}" />`
        + `<span class="col-rows">`
        + `<button type="button" class="rows-btn" data-rowsminus="${esc(col.id)}" title="Fewer rows">&minus;</button>`
        + `<span class="rows-count">${rows}</span>`
        + `<button type="button" class="rows-btn" data-rowsplus="${esc(col.id)}" title="More rows">+</button>`
        + `</span>`
        + `<button type="button" class="col-del" data-delcol="${esc(col.id)}" title="Delete column">&times;</button>`
        + `</div>`;
    } else {
      html += `<div class="col-head"><span><span class="num">${ci + 1}</span>&nbsp; ${esc(col.name)}</span><span>${count} ${count === 1 ? "cue" : "cues"}</span></div>`;
    }

    for (let r = 0; r < rows; r++) {
      const p = store.placementAt(page.id, col.id, r);
      if (!p) {
        if (editMode) {
          html += `<div class="cell empty editable" data-col="${esc(col.id)}" data-row="${r}">+ empty +</div>`;
        } else {
          html += `<div class="cell empty">&mdash; empty &mdash;</div>`;
        }
        continue;
      }
      // Cursor sits before its standby cell.
      if (showCursor && standbyPid === p.id) {
        html += `<div class="cursor"><span class="chev">${CHEV}</span><div class="line"></div></div>`;
      }
      html += cellHTML(p, playingPid, bgById, played, col.id, r, editMode);
    }
    html += `</div>`;
  }
  grid.innerHTML = html;

  // Refresh per-frame targets.
  els.fill = grid.querySelector(".cell.playing .fill");
  els.live = grid.querySelector(".cell.playing .meta .live");

  // Rebuild the background running-time cache: the cells were just replaced,
  // so old references are stale. Cells that stopped are simply absent now.
  bgMetaEls = new Map();
  bgMetaText = new Map();
  grid.querySelectorAll(".cell.bgcue[data-pid]").forEach((cell) => {
    const meta = cell.querySelector(".meta");
    if (meta) {
      bgMetaEls.set(cell.dataset.pid, meta);
      bgMetaText.set(cell.dataset.pid, meta.textContent);
    }
  });

  if (editMode) wireGridEditControls(grid, page);

  maybeScrollCursorIntoView(s);
}

// Scroll the standby cursor into view after a render, but only when its
// position changed (page + standby placement id) and the operator is not
// actively touching / moving a cue -- so edit toggles and library renames
// don't yank the scroll position.
function maybeScrollCursorIntoView(s) {
  const rt = s.runtime;
  const key = (rt ? rt.currentPage : "") + "|" + (store.standbyPlacementId() || "");
  if (key === lastCursorKey) return;
  lastCursorKey = key;
  if (press.pid != null || tmove.pid != null) return;
  const cur = els.grid.querySelector(".cursor");
  if (cur) cur.scrollIntoView({ block: "nearest", inline: "nearest", behavior: "smooth" });
}

function cellHTML(p, playingPid, bgById, played, colId, row, editMode) {
  const item = store.libraryItem(p.libraryItemId);
  const name = item ? item.name : "(missing)";
  const pid = esc(p.id);
  const posAttrs = ` data-col="${esc(colId)}" data-row="${row}"`;
  const dragAttr = editMode ? ` draggable="true"` : "";
  const removeBtn = editMode
    ? `<button type="button" class="cell-remove" data-remove="${pid}" title="Remove from grid">&times;</button>`
    : "";

  if (p.id === playingPid) {
    return `<div class="cell playing" data-pid="${pid}"${posAttrs}${dragAttr}>`
      + `<div class="fill"></div>`
      + `<div class="name"><span class="label">${esc(name)}</span><span class="chip">Playing</span></div>`
      + `<div class="meta"><span class="live">-0:00</span></div>`
      + removeBtn
      + `</div>`;
  }

  const bg = bgById.get(p.id);
  if (bg) {
    const loop = bg.loop ? `<span class="loop">${LOOP_ICON}</span>` : "";
    const run = formatClock(framesToSeconds(bg.frame));
    return `<div class="cell bgcue" data-pid="${pid}"${posAttrs}${dragAttr}>`
      + `<div class="name">${loop}<span class="label">${esc(name)}</span><span class="tag">bg</span></div>`
      + `<div class="meta">${run}</div>`
      + removeBtn
      + `</div>`;
  }

  const icon = iconHTML(item);
  const typeCls = typeClass(item);
  const typeTag = typeTagHTML(item);
  if (played.has(p.id)) {
    return `<div class="cell played${typeCls}" data-pid="${pid}"${posAttrs}${dragAttr}>`
      + `<div class="name"><span class="check">${CHECK}</span>${icon}<span class="label">${esc(name)}</span>${typeTag}</div>`
      + `<div class="meta">${metaText(item)}</div>`
      + removeBtn
      + `</div>`;
  }

  return `<div class="cell${typeCls}" data-pid="${pid}"${posAttrs}${dragAttr}>`
    + `<div class="name">${icon}<span class="label">${esc(name)}</span>${typeTag}</div>`
    + `<div class="meta">${metaText(item)}</div>`
    + removeBtn
    + `</div>`;
}

// Column-header controls: rewired after each edit-mode grid render
// (grid.innerHTML is fully replaced each time it is called).
function wireGridEditControls(grid, page) {
  function findColumn(colId) {
    return (page.columns || []).find((c) => c.id === colId) || null;
  }
  function columnCueCount(colId) {
    return (store.show().placements || []).filter((p) => p.column === colId).length;
  }

  grid.querySelectorAll("[data-colname]").forEach((input) => {
    input.addEventListener("click", (e) => e.stopPropagation());
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") input.blur();
      if (e.key === "Escape") {
        const col = findColumn(input.dataset.colname);
        if (col) input.value = col.name;
        input.blur();
      }
    });
    input.addEventListener("blur", () => {
      const colId = input.dataset.colname;
      const col = findColumn(colId);
      const v = input.value.trim();
      if (col && v && v !== col.name) send("renameColumn", { columnId: colId, name: v });
    });
  });

  grid.querySelectorAll("[data-rowsminus]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const colId = btn.dataset.rowsminus;
      const col = findColumn(colId);
      if (col) send("setRows", { columnId: colId, rows: Math.max(1, (col.rows | 0) - 1) });
    });
  });
  grid.querySelectorAll("[data-rowsplus]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const colId = btn.dataset.rowsplus;
      const col = findColumn(colId);
      if (col) send("setRows", { columnId: colId, rows: (col.rows | 0) + 1 });
    });
  });
  grid.querySelectorAll("[data-delcol]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const colId = btn.dataset.delcol;
      const col = findColumn(colId);
      const count = columnCueCount(colId);
      const name = col ? col.name : "this column";
      if (await confirmDialog(`Delete column "${name}"? It has ${count} cue${count === 1 ? "" : "s"}.`, { danger: true })) {
        send("removeColumn", { columnId: colId });
      }
    });
  });
}

// ---------------------------------------------------------------- now playing

function renderNowPlaying(s) {
  const rt = s.runtime;
  // Now-playing name.
  let name;
  if (rt && rt.playing) {
    const p = store.placement(rt.playing.placementId);
    const item = p ? store.libraryItem(p.libraryItemId) : null;
    const nm = item ? item.name : "(playing)";
    name = `<span class="play">${PLAY_TRI}</span> ${esc(nm)}`;
  } else {
    name = "Idle";
  }

  // Next (standby) name.
  const standbyPid = store.standbyPlacementId();
  let next;
  if (standbyPid) {
    const p = store.placement(standbyPid);
    const item = p ? store.libraryItem(p.libraryItemId) : null;
    const nm = item ? item.name : "(cue)";
    next = `next ${NEXT_TRI} <span class="name">${esc(nm)}</span>`;
  } else {
    next = `next ${NEXT_TRI} <span class="name">end of page</span>`;
  }

  if (name !== lastNp.name) {
    els.npname.innerHTML = name;
    lastNp.name = name;
  }
  if (next !== lastNp.next) {
    els.npnext.innerHTML = next;
    lastNp.next = next;
  }
}

// ---------------------------------------------------------------- RAF loop

// Last texts written by the rAF loop -- they change once per second, so
// skipping the ~59 no-op writes/sec avoids needless style/layout work.
let lastTimerText = null;
let lastLiveText = null;

function frame() {
  if (!els) return;

  // Smooth timer + playing cell fill.
  if (sync.pid) {
    const elapsed = (performance.now() - sync.at) / 1000;
    const remaining = Math.max(0, sync.remainAt - elapsed);
    const timerText = formatTimer(remaining);
    if (timerText !== lastTimerText) {
      lastTimerText = timerText;
      els.timer.textContent = timerText;
    }
    if (els.live) {
      const liveText = "-" + formatClock(remaining);
      if (liveText !== lastLiveText) {
        lastLiveText = liveText;
        els.live.textContent = liveText;
      }
    }
    if (els.fill) {
      const prog = sync.total > 0
        ? Math.min(1, Math.max(0, (sync.total - remaining) / sync.total))
        : 0;
      // translateX on a full-width layer is compositor-only; animating width
      // would relayout the cell every frame (visible as choppiness).
      els.fill.style.transform =
        "translateX(" + ((prog - 1) * 100).toFixed(3) + "%)";
    }
  } else {
    const timerText = formatTimer(0);
    if (timerText !== lastTimerText) {
      lastTimerText = timerText;
      els.timer.textContent = timerText;
    }
  }

  // GO lock / connection state.
  const locked = performance.now() < goLockUntil;
  els.go.disabled = locked || !isConnected();

  requestAnimationFrame(frame);
}
