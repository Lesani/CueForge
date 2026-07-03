// CueForge Library section: item list (left) + per-item editor (right).
// Reads from the store, writes via ws.send(updateLibraryItem/...)
// and the shared importer helper (dedup modal + REST import).

import * as store from "./store.js";
import { send, getPin } from "./ws.js";
import { esc, itemDuration, formatClock, framesToSeconds } from "./util.js";
import { importFileWithDedup } from "./importer.js";
import { openYouTubeImport } from "./youtube.js";
import { confirmDialog, alertDialog } from "./dialogs.js";
import { createWaveform } from "./waveform.js";
import { getAudioBuffer, getAudioContext } from "./audioCache.js";

let sectionEl = null;
let els = null;
let unsub = null;
let selectedId = null;

// List filter (name substring, case-insensitive) and the show it applies to --
// reset whenever the active show changes.
let filterText = "";
let filterForShow = null;

// List sort mode: "name" (default) | "type" | "duration" | "group".
let sortMode = "name";

// Collapsed group names (client-local UI state): survives re-renders, resets on
// show change alongside the filter. The ungrouped bucket uses the empty-string
// key. Included in listSig so toggling a header re-renders through the gate.
let collapsedGroups = new Set();

// Sentinel <option> value for the editor's "New group..." choice.
const NEW_GROUP_OPT = "::new::";

// "+ Stop cue" auto-select: after createStopCue we watch for a brand-new stop
// item (an id not seen in the previous render) and select it once it arrives.
let pendingStopSelect = false;
let prevItemIds = new Set();

// Editor rebuild guard: only tear down / recreate the type-specific DOM
// (and the waveform widget) when the selected item's id/type/audio changes.
let editorBuiltFor = null;
let waveformHandle = null;

// "This device" audition playback state. startTime/trimIn/trimOut are the
// WebAudio clock + absolute waveform bounds used to place the playhead marker.
let deviceAudition = { source: null, gainNode: null, playing: false, startTime: 0, trimIn: 0, trimOut: 0 };
// "Server out" playhead as a local wall clock anchored to the status stream:
// { itemId, atPerf0, played0, total }. Playback is deterministic (exactly 1.0x
// real time), and the reported engine frame only advances per audio-callback
// block (so several 15 Hz ticks repeat the same frame, then it jumps). We
// therefore anchor ONCE and advance purely by wall time; the frame is used only
// to detect start/stop and to re-anchor on a discontinuity (loop wrap / seek).
let serverPlayhead = null;
// Frame delta (s) beyond which we treat the report as a discontinuity and
// re-anchor rather than trust the smooth local clock.
const PLAYHEAD_JUMP_S = 0.25;
// Local (client-only) toggle state for the audition row -- the server does
// not report audition-channel playback status, so "server out" Play/Stop is
// tracked optimistically per selection.
let auditionTarget = "server";
let serverAuditionPlaying = false;

// Gate the list rebuild on an actual content change -- the store fires at
// 15 Hz (status tick) and a full innerHTML replace that often is wasteful
// (and would fight any future in-list editing), so only rebuild on change.
let lastListSig = null;

// ---------------------------------------------------------------- mount

export function mount(container) {
  sectionEl = container;
  sectionEl.innerHTML = `
    <div class="lib-body">
      <div class="lib-list-pane">
        <div class="lib-toolbar">
          <button class="btn primary" type="button" data-import>Import</button>
          <button class="btn" type="button" data-yt-import>YouTube</button>
          <button class="btn" type="button" data-stop-cue>Stop cue</button>
          <input type="file" data-file-input multiple hidden accept="audio/*,.wav,.mp3,.flac,.aac,.ogg,.m4a" />
          <div class="lib-count" data-count>0 items</div>
        </div>
        <div class="lib-filter-row">
          <select class="lib-sort" data-sort title="Sort by">
            <option value="name">Name</option>
            <option value="type">Type</option>
            <option value="duration">Duration</option>
            <option value="group">Group</option>
          </select>
          <div class="lib-filter-wrap">
            <input type="text" class="lib-filter" data-filter placeholder="Filter..." />
            <button type="button" class="lib-filter-clear" data-filter-clear hidden title="Clear filter">×</button>
          </div>
        </div>
        <div class="lib-list" data-list></div>
      </div>
      <div class="lib-editor-pane" data-editor>
        <div class="lib-empty">Select a library item to edit it</div>
      </div>
    </div>`;

  els = {
    importBtn: sectionEl.querySelector("[data-import]"),
    ytBtn: sectionEl.querySelector("[data-yt-import]"),
    stopCueBtn: sectionEl.querySelector("[data-stop-cue]"),
    sort: sectionEl.querySelector("[data-sort]"),
    fileInput: sectionEl.querySelector("[data-file-input]"),
    count: sectionEl.querySelector("[data-count]"),
    filter: sectionEl.querySelector("[data-filter]"),
    filterClear: sectionEl.querySelector("[data-filter-clear]"),
    list: sectionEl.querySelector("[data-list]"),
    editor: sectionEl.querySelector("[data-editor]"),
  };

  els.importBtn.addEventListener("click", () => els.fileInput.click());
  els.ytBtn.addEventListener("click", () => openYouTubeImport());
  els.stopCueBtn.addEventListener("click", () => {
    pendingStopSelect = true;
    send("createStopCue", {});
  });
  els.sort.value = sortMode;
  els.sort.addEventListener("change", () => {
    sortMode = els.sort.value;
    render(store.get());
  });
  els.fileInput.addEventListener("change", async () => {
    const files = Array.from(els.fileInput.files || []);
    els.fileInput.value = "";
    for (const file of files) {
      await importFileWithDedup(file, null);
    }
  });
  els.filter.addEventListener("input", () => {
    filterText = els.filter.value;
    els.filterClear.hidden = filterText.length === 0;
    render(store.get());
  });
  els.filterClear.addEventListener("click", () => {
    filterText = "";
    els.filter.value = "";
    els.filterClear.hidden = true;
    els.filter.focus();
    render(store.get());
  });

  unsub = store.subscribe(render);
  render(store.get());
}

// Force a re-render (e.g. when the Library tab becomes active).
export function refresh() {
  render(store.get());
}

function libraryItems(show) {
  if (!show || !show.library) return [];
  return sortItems(Object.values(show.library));
}

// Sort rank for the "Type" order: normal, then background, then stop.
function typeRank(t) {
  return t === "normal" ? 0 : t === "background" ? 1 : 2;
}

// Sort a copy of the items per the active sortMode. Every mode falls back to
// name for a stable, predictable order within ties.
function sortItems(items) {
  const byName = (a, b) => a.name.localeCompare(b.name);
  const arr = items.slice();
  if (sortMode === "type") {
    arr.sort((a, b) => (typeRank(a.type) - typeRank(b.type)) || byName(a, b));
  } else if (sortMode === "duration") {
    arr.sort((a, b) => {
      const as = a.type === "stop", bs = b.type === "stop";
      if (as !== bs) return as ? 1 : -1;          // stop items last
      if (as && bs) return byName(a, b);
      return ((itemDuration(b) || 0) - (itemDuration(a) || 0)) || byName(a, b); // longest first
    });
  } else if (sortMode === "group") {
    arr.sort((a, b) => {
      const ga = (a.group || "").trim(), gb = (b.group || "").trim();
      if (!!ga !== !!gb) return ga ? -1 : 1;        // ungrouped last
      return ga.localeCompare(gb) || byName(a, b);  // group A-Z, then name
    });
  } else {
    arr.sort(byName);
  }
  return arr;
}

// Distinct, non-empty group names across the library (for the editor dropdown).
function groupNames(show) {
  const set = new Set();
  for (const it of Object.values((show && show.library) || {})) {
    const g = (it.group || "").trim();
    if (g) set.add(g);
  }
  return [...set].sort((a, b) => a.localeCompare(b));
}

// The editor's group <select> options: "(none)", every distinct group name
// (sorted), then a trailing "New group..." sentinel.
function groupSelectOptions(show, item) {
  const cur = (item.group || "").trim();
  let html = `<option value=""${cur === "" ? " selected" : ""}>(none)</option>`;
  for (const g of groupNames(show)) {
    html += `<option value="${esc(g)}"${g === cur ? " selected" : ""}>${esc(g)}</option>`;
  }
  html += `<option value="${NEW_GROUP_OPT}">New group...</option>`;
  return html;
}

function placementCount(show, libraryItemId) {
  if (!show || !Array.isArray(show.placements)) return 0;
  let n = 0;
  for (const p of show.placements) if (p.libraryItemId === libraryItemId) n++;
  return n;
}

function metaText(item) {
  if (!item) return "";
  if (item.type === "stop") return "STOP";
  const d = itemDuration(item);
  return d != null ? formatClock(d) : "--:--";
}

// ---------------------------------------------------------------- render

function render(s) {
  if (!els || !sectionEl) return;
  if (sectionEl.hidden) return; // not the active tab -- skip work
  const show = s.show;
  const items = libraryItems(show);

  const showKey = show ? show.name : null;
  if (showKey !== filterForShow) {
    filterForShow = showKey;
    filterText = "";
    collapsedGroups = new Set();
    if (els.filter) els.filter.value = "";
    if (els.filterClear) els.filterClear.hidden = true;
  }

  if (selectedId && !items.some((it) => it.id === selectedId)) selectedId = null;
  if (!selectedId && items.length) selectedId = items[0].id;

  // Auto-select a freshly-created stop cue once its snapshot lands. Race-
  // tolerant: if nothing new appears yet the flag simply waits for the next
  // new stop item.
  if (pendingStopSelect) {
    const fresh = items.find((it) => it.type === "stop" && !prevItemIds.has(it.id));
    if (fresh) { selectedId = fresh.id; pendingStopSelect = false; }
  }
  prevItemIds = new Set(items.map((it) => it.id));

  // Mirror the authoritative server-out audition status from the runtime so the
  // Play/Stop button flips back to "Play" when server playback finishes (the
  // server does not otherwise notify per-cue completion).
  if (auditionTarget === "server") {
    serverAuditionPlaying = !!(s.runtime && s.runtime.auditionActive);
  }

  // Feed the server-out audition position into the monotonic playhead clock.
  const aud = s.runtime && s.runtime.audition;
  if (aud && aud.libraryItemId) {
    updateServerPlayhead(aud.libraryItemId, framesToSeconds(aud.frame || 0), framesToSeconds(aud.totalFrames || 0));
  } else {
    serverPlayhead = null;
  }

  const sig = listSig(items);
  if (sig !== lastListSig) {
    lastListSig = sig;
    renderList(show, items);
  }
  renderEditor(show, selectedId ? store.libraryItem(selectedId) : null);
  updatePlayButton();
  updatePlayheadMarker();
}

// Whether the waveform marker should be sweeping right now, and (via the getter
// passed to the widget) where. Device uses the exact WebAudio clock; server uses
// the extrapolated status position, gated on the auditioning item being the one
// this client has selected.
function updatePlayheadMarker() {
  if (!waveformHandle) return;
  const show =
    (auditionTarget === "device" && deviceAudition.playing) ||
    (auditionTarget === "server" && !!serverPlayhead && serverPlayhead.itemId === selectedId);
  waveformHandle.setPlaying(show);
}

// Anchor the server playhead clock from a status tick. Anchor once per playback;
// only re-anchor when the item changes or the report diverges from the local
// clock by more than PLAYHEAD_JUMP_S (loop wrap / seek / gross desync). Otherwise
// leave the anchor untouched so the marker is pure, deterministic wall time.
function updateServerPlayhead(itemId, played, total) {
  const now = performance.now();
  const p = serverPlayhead;
  if (!p || p.itemId !== itemId) {
    serverPlayhead = { itemId, atPerf0: now, played0: played, total };
    return;
  }
  const predicted = p.played0 + (now - p.atPerf0) / 1000;
  if (Math.abs(played - predicted) > PLAYHEAD_JUMP_S) {
    p.atPerf0 = now;
    p.played0 = played;
  }
  p.total = total;
}

function playheadSeconds() {
  const item = currentItem();
  if (!item) return null;
  if (auditionTarget === "device") {
    if (!deviceAudition.playing) return null;
    try {
      const ctx = getAudioContext();
      const t = deviceAudition.trimIn + (ctx.currentTime - deviceAudition.startTime);
      return Math.min(t, deviceAudition.trimOut);
    } catch { return null; }
  }
  const p = serverPlayhead;
  if (!p || p.itemId !== selectedId) return null;
  const trimIn = Math.max(0, Number(item.trimIn) || 0);
  const predicted = p.played0 + (performance.now() - p.atPerf0) / 1000;
  return trimIn + Math.max(0, Math.min(p.total, predicted));
}

function listSig(items) {
  let s = selectedId + "|" + filterText + "|" + sortMode + "|" +
    [...collapsedGroups].sort().join(",") + "|";
  for (const it of items) {
    s += it.id + it.name + it.type + it.duration + it.trimIn + it.trimOut + (it.group || "") + ";";
  }
  return s;
}

function matchesFilter(it) {
  return !filterText || it.name.toLowerCase().includes(filterText.toLowerCase());
}

function rowHtml(it) {
  const sel = it.id === selectedId ? " selected" : "";
  return `
      <div class="lib-row${sel}" data-row="${esc(it.id)}">
        <div class="info">
          <div class="name">${esc(it.name)}</div>
          <div class="sub">
            <span class="type-badge ${esc(it.type)}">${esc(it.type)}</span>
            <span class="duration">${esc(metaText(it))}</span>
          </div>
        </div>
        <div class="row-actions">
          <button type="button" data-dup="${esc(it.id)}" title="Duplicate">Dup</button>
          <button type="button" class="danger" data-del="${esc(it.id)}" title="Delete">Del</button>
        </div>
      </div>`;
}

function renderList(show, items) {
  const visible = items.filter(matchesFilter);
  els.count.textContent = filterText
    ? `${visible.length} of ${items.length} items`
    : `${items.length} ${items.length === 1 ? "item" : "items"}`;

  if (!items.length) {
    els.list.innerHTML = `<div class="lib-empty-hint">No library items yet. Import an audio file to get started.</div>`;
    return;
  }
  if (!visible.length) {
    els.list.innerHTML = `<div class="lib-empty-hint">No items match "${esc(filterText)}".</div>`;
    return;
  }

  // Group under clickable, collapsible headers when items are grouped and the
  // order keeps a group's items contiguous ("Name" or "Group"). The ungrouped
  // bucket uses the empty-string key and renders last as "No group".
  const anyGroup = items.some((it) => (it.group || "").trim());
  const grouped = anyGroup && (sortMode === "name" || sortMode === "group");

  if (grouped) {
    const groupKey = (it) => (it.group || "").trim();

    // Never leave the selection hidden inside a collapsed group: auto-expand
    // the selected item's group before rendering.
    const selItem = visible.find((it) => it.id === selectedId);
    if (selItem) collapsedGroups.delete(groupKey(selItem));

    const groups = new Map();
    for (const it of visible) {
      const key = groupKey(it);
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(it);
    }
    const keys = [...groups.keys()].filter((k) => k !== "").sort((a, b) => a.localeCompare(b));
    if (groups.has("")) keys.push("");
    els.list.innerHTML = keys.map((key) => {
      const rows = groups.get(key);
      const label = key === "" ? "No group" : key;
      const collapsed = collapsedGroups.has(key);
      const chevron = collapsed ? "▸" : "▾";  // triangle right / down
      const head =
        `<div class="lib-group-head${collapsed ? " collapsed" : ""}" data-group-key="${esc(key)}">` +
          `<span class="chev">${chevron}</span>` +
          `<span class="group-name">${esc(label)}</span>` +
          `<span class="group-count">${rows.length}</span>` +
        `</div>`;
      return head + (collapsed ? "" : rows.map(rowHtml).join(""));
    }).join("");
  } else {
    els.list.innerHTML = visible.map(rowHtml).join("");
  }

  els.list.querySelectorAll("[data-group-key]").forEach((head) => {
    head.addEventListener("click", () => {
      const key = head.dataset.groupKey;
      if (collapsedGroups.has(key)) collapsedGroups.delete(key);
      else collapsedGroups.add(key);
      render(store.get());
    });
  });
  els.list.querySelectorAll("[data-row]").forEach((row) => {
    row.addEventListener("click", (e) => {
      if (e.target.closest("[data-dup],[data-del]")) return;
      if (row.dataset.row === selectedId) return;
      if (serverAuditionPlaying) send("stopAudition");
      selectedId = row.dataset.row;
      stopDeviceAudition();
      serverAuditionPlaying = false;
      render(store.get());
    });
  });
  els.list.querySelectorAll("[data-dup]").forEach((btn) => {
    btn.addEventListener("click", () => send("duplicateLibraryItem", { libraryItemId: btn.dataset.dup }));
  });
  els.list.querySelectorAll("[data-del]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const id = btn.dataset.del;
      const item = store.libraryItem(id);
      const n = placementCount(show, id);
      const name = item ? item.name : "this item";
      const usage = n > 0 ? ` It is used by ${n} placement${n === 1 ? "" : "s"}, which will be removed too.` : "";
      if (await confirmDialog(`Delete "${name}"?${usage}`, { danger: true })) {
        if (selectedId === id) selectedId = null;
        send("deleteLibraryItem", { libraryItemId: id });
      }
    });
  });
}

// ---------------------------------------------------------------- editor

function debounced(fn, ms) {
  let t = null;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
}

function updateItem(libraryItemId, fields) {
  send("updateLibraryItem", { libraryItemId, fields });
}

function isFocused(el) {
  return el && document.activeElement === el;
}

function backgroundOptions(show, excludeId) {
  return libraryItems(show).filter((it) => it.type === "background" && it.id !== excludeId);
}

function renderEditor(show, item) {
  if (!item) {
    editorBuiltFor = null;
    if (waveformHandle) { waveformHandle.destroy(); waveformHandle = null; }
    els.editor.innerHTML = `<div class="lib-empty">Select a library item to edit it</div>`;
    return;
  }

  const key = item.id + "|" + item.type + "|" + item.audioHash;
  if (key !== editorBuiltFor) {
    buildEditor(show, item);
    editorBuiltFor = key;
  } else {
    syncEditor(item);
  }
}

function buildEditor(show, item) {
  if (waveformHandle) { waveformHandle.destroy(); waveformHandle = null; }

  const isStop = item.type === "stop";
  const isBackground = item.type === "background";

  let html = `
    <div class="lib-editor-head">
      <input type="text" class="name-input" data-f-name value="${esc(item.name)}" />
      <select class="type-select" data-f-type>
        <option value="normal"${item.type === "normal" ? " selected" : ""}>Normal</option>
        <option value="background"${item.type === "background" ? " selected" : ""}>Background</option>
        <option value="stop"${item.type === "stop" ? " selected" : ""}>Stop</option>
      </select>
    </div>
    <div class="lib-group-row">
      <label>Group</label>
      <select class="group-select" data-f-group>${groupSelectOptions(show, item)}</select>
      <input type="text" class="group-new-input" data-f-group-new placeholder="New group name" hidden />
    </div>`;

  if (!isStop) {
    html += `
      <div class="waveform-section">
        <div class="lib-field-label">Waveform (drag handles to trim)</div>
        <div class="waveform-host" data-waveform></div>
        <div class="waveform-hint">Trim: <span data-trim-readout></span></div>
      </div>
      <div class="lib-field-group">
        <div class="lib-field">
          <label>Gain (dB)</label>
          <div class="gain-row">
            <input type="range" min="-48" max="12" step="0.1" data-f-gain-range value="${Number(item.gainDb) || 0}" />
            <input type="number" min="-48" max="12" step="0.1" data-f-gain-num value="${Number(item.gainDb) || 0}" />
          </div>
        </div>
        <div class="lib-field">
          <label>Fade in (s)</label>
          <input type="number" min="0" step="0.05" data-f-fadein value="${Number(item.fadeIn) || 0}" />
        </div>
        <div class="lib-field">
          <label>Fade out (s)</label>
          <input type="number" min="0" step="0.05" data-f-fadeout value="${Number(item.fadeOut) || 0}" />
        </div>
        <div class="lib-field">
          <label>Fade shape</label>
          <select data-f-fadeshape>
            <option value="linear"${item.fadeShape === "linear" ? " selected" : ""}>Linear</option>
            <option value="equalPower"${item.fadeShape === "equalPower" ? " selected" : ""}>Equal power</option>
          </select>
        </div>
        <div class="lib-field">
          <label>&nbsp;</label>
          <button class="btn" type="button" data-normalize>Normalize (~-1 dBFS)</button>
        </div>
      </div>`;
    if (isBackground) {
      html += `
      <div class="lib-toggle-row">
        <label><input type="checkbox" data-f-loop ${item.loop ? "checked" : ""} /> Loop seamlessly</label>
      </div>`;
    }
  } else {
    const bgOptions = backgroundOptions(show, item.id).map((bg) =>
      `<option value="${esc(bg.id)}"${item.stopTarget === bg.id ? " selected" : ""}>${esc(bg.name)}</option>`
    ).join("");
    html += `
      <div class="lib-field-group">
        <div class="lib-field">
          <label>Stop target</label>
          <select data-f-stoptarget>
            <option value="allBackgrounds"${item.stopTarget === "allBackgrounds" ? " selected" : ""}>All backgrounds</option>
            ${bgOptions}
          </select>
        </div>
        <div class="lib-field">
          <label>Stop mode</label>
          <select data-f-stopmode>
            <option value="hard"${item.stopMode === "hard" ? " selected" : ""}>Hard</option>
            <option value="fade"${item.stopMode === "fade" ? " selected" : ""}>Fade</option>
          </select>
        </div>
        <div class="lib-field" data-fadesecs-field ${item.stopMode === "fade" ? "" : "hidden"}>
          <label>Fade seconds</label>
          <input type="number" min="0" step="0.1" data-f-stopfadesecs value="${Number(item.stopFadeSeconds) || 0}" />
        </div>
      </div>`;
  }

  html += `
    <div class="audition-row">
      <div class="audition-target">
        <button type="button" data-target="server" class="${auditionTarget === "server" ? "active" : ""}">Server out</button>
        <button type="button" data-target="device" class="${auditionTarget === "device" ? "active" : ""}"${isStop ? " disabled" : ""}>This device</button>
      </div>
      <button type="button" class="audition-play" data-play${isStop ? " disabled" : ""}>Play</button>
      <div class="audition-note">"This device" is an approximate JS preview (trim/gain/fades only).</div>
    </div>`;

  if (!isStop) {
    const dis = item.audioHash ? "" : " disabled";
    html += `
      <div class="export-row">
        <span class="export-label">Export</span>
        <button type="button" class="export-btn" data-export="wav"${dis}>WAV</button>
        <button type="button" class="export-btn" data-export="flac"${dis}>FLAC</button>
        <button type="button" class="export-btn" data-export="mp3"${dis}>MP3</button>
      </div>`;
  }

  els.editor.innerHTML = html;

  wireEditorInputs(item, isStop);

  if (!isStop) {
    const host = els.editor.querySelector("[data-waveform]");
    waveformHandle = createWaveform(host, {
      audioHash: item.audioHash,
      duration: Number(item.duration) || 0,
      trimIn: Number(item.trimIn) || 0,
      trimOut: Number(item.trimOut) || 0,
      getPlayheadSeconds: playheadSeconds,
      onTrimChange: ({ trimIn, trimOut }) => {
        // Stop audition on a trim edit so the marker can't drift against the
        // freshly-changed bounds; it resumes correctly on the next Play.
        stopAuditionForTrim();
        updateItem(item.id, { trimIn, trimOut });
        updateTrimReadout(trimIn, trimOut);
      },
    });
    updateTrimReadout(Number(item.trimIn) || 0, Number(item.trimOut) > 0 ? Number(item.trimOut) : Number(item.duration) || 0);
  }
}

function updateTrimReadout(trimIn, trimOut) {
  const el = els.editor.querySelector("[data-trim-readout]");
  if (el) el.textContent = `${formatClock(trimIn)} → ${formatClock(trimOut)}`;
}

function wireEditorInputs(item, isStop) {
  const ed = els.editor;

  const nameInput = ed.querySelector("[data-f-name]");
  if (nameInput) {
    const commit = () => {
      const v = nameInput.value.trim();
      if (v && v !== item.name) updateItem(item.id, { name: v });
    };
    nameInput.addEventListener("blur", commit);
    nameInput.addEventListener("keydown", (e) => { if (e.key === "Enter") nameInput.blur(); });
  }

  const typeSelect = ed.querySelector("[data-f-type]");
  if (typeSelect) {
    typeSelect.addEventListener("change", () => {
      updateItem(item.id, { type: typeSelect.value });
    });
  }

  const groupSelect = ed.querySelector("[data-f-group]");
  const groupNewInput = ed.querySelector("[data-f-group-new]");
  if (groupSelect && groupNewInput) {
    // Last committed selection, so cancelling "New group..." can revert to it.
    let prevValue = (item.group || "").trim();
    groupSelect.addEventListener("change", () => {
      if (groupSelect.value === NEW_GROUP_OPT) {
        groupNewInput.hidden = false;
        groupNewInput.value = "";
        groupNewInput.focus();
        return;
      }
      groupNewInput.hidden = true;
      prevValue = groupSelect.value;
      if (groupSelect.value !== (item.group || "")) updateItem(item.id, { group: groupSelect.value });
    });
    const commitNew = () => {
      if (groupNewInput.hidden) return;
      const v = groupNewInput.value.trim();
      groupNewInput.hidden = true;
      if (v) {
        // Add the option if new, then select + commit it.
        if (![...groupSelect.options].some((o) => o.value === v)) {
          const opt = document.createElement("option");
          opt.value = v;
          opt.textContent = v;
          groupSelect.insertBefore(opt, groupSelect.lastElementChild);
        }
        groupSelect.value = v;
        prevValue = v;
        if (v !== (item.group || "")) updateItem(item.id, { group: v });
      } else {
        groupSelect.value = prevValue;  // empty -> revert
      }
    };
    groupNewInput.addEventListener("blur", commitNew);
    groupNewInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); groupNewInput.blur(); }
      else if (e.key === "Escape") { groupNewInput.value = ""; groupNewInput.blur(); }
    });
  }

  if (!isStop) {
    const gainRange = ed.querySelector("[data-f-gain-range]");
    const gainNum = ed.querySelector("[data-f-gain-num]");
    const sendGain = debounced((v) => updateItem(item.id, { gainDb: v }), 150);
    const onGain = (v) => {
      const n = Number(v);
      if (gainRange) gainRange.value = n;
      if (gainNum) gainNum.value = n;
      applyLiveDeviceGain(n);   // live-adjust an in-progress "this device" preview
      sendGain(n);
    };
    if (gainRange) gainRange.addEventListener("input", () => onGain(gainRange.value));
    if (gainNum) gainNum.addEventListener("input", () => onGain(gainNum.value));

    const fadeIn = ed.querySelector("[data-f-fadein]");
    const sendFadeIn = debounced((v) => updateItem(item.id, { fadeIn: v }), 250);
    if (fadeIn) fadeIn.addEventListener("input", () => sendFadeIn(Math.max(0, Number(fadeIn.value) || 0)));

    const fadeOut = ed.querySelector("[data-f-fadeout]");
    const sendFadeOut = debounced((v) => updateItem(item.id, { fadeOut: v }), 250);
    if (fadeOut) fadeOut.addEventListener("input", () => sendFadeOut(Math.max(0, Number(fadeOut.value) || 0)));

    const fadeShape = ed.querySelector("[data-f-fadeshape]");
    if (fadeShape) fadeShape.addEventListener("change", () => updateItem(item.id, { fadeShape: fadeShape.value }));

    const normBtn = ed.querySelector("[data-normalize]");
    if (normBtn) normBtn.addEventListener("click", () => send("normalizeItem", { libraryItemId: item.id }));

    const loop = ed.querySelector("[data-f-loop]");
    if (loop) loop.addEventListener("change", () => updateItem(item.id, { loop: loop.checked }));

    // Export: trigger a browser download via a transient <a download> (avoids
    // popup blockers that would kill window.open). The server bakes in
    // trim/gain/fades and sets Content-Disposition.
    ed.querySelectorAll("[data-export]").forEach((btn) => {
      btn.addEventListener("click", () => {
        if (btn.disabled) return;
        const a = document.createElement("a");
        // A <a download> cannot send headers, so remote clients authenticate the
        // REST export via a PIN query param (localhost returns "" -> omitted).
        let href = `/api/export/${encodeURIComponent(item.id)}?format=${btn.dataset.export}`;
        const pin = getPin();
        if (pin) href += `&pin=${encodeURIComponent(pin)}`;
        a.href = href;
        a.download = "";
        document.body.appendChild(a);
        a.click();
        a.remove();
      });
    });
  } else {
    const stopTarget = ed.querySelector("[data-f-stoptarget]");
    if (stopTarget) stopTarget.addEventListener("change", () => updateItem(item.id, { stopTarget: stopTarget.value }));

    const stopMode = ed.querySelector("[data-f-stopmode]");
    const fadeSecsField = ed.querySelector("[data-fadesecs-field]");
    if (stopMode) {
      stopMode.addEventListener("change", () => {
        updateItem(item.id, { stopMode: stopMode.value });
        if (fadeSecsField) fadeSecsField.hidden = stopMode.value !== "fade";
      });
    }
    const stopFadeSecs = ed.querySelector("[data-f-stopfadesecs]");
    const sendStopFade = debounced((v) => updateItem(item.id, { stopFadeSeconds: v }), 250);
    if (stopFadeSecs) stopFadeSecs.addEventListener("input", () => sendStopFade(Math.max(0, Number(stopFadeSecs.value) || 0)));
  }

  wireAudition(item, isStop);
}

function syncEditor(item) {
  const ed = els.editor;
  const set = (sel, prop, val) => {
    const el = ed.querySelector(sel);
    if (el && !isFocused(el)) el[prop] = val;
  };
  set("[data-f-name]", "value", item.name);
  set("[data-f-type]", "value", item.type);

  // Rebuild the group dropdown (picks up groups added on other items) and
  // reselect, but only when idle: don't clobber focus or an in-progress
  // "New group..." entry.
  const groupSelect = ed.querySelector("[data-f-group]");
  const groupNewInput = ed.querySelector("[data-f-group-new]");
  if (groupSelect && !isFocused(groupSelect) && (!groupNewInput || groupNewInput.hidden)) {
    groupSelect.innerHTML = groupSelectOptions(store.show(), item);
  }

  if (item.type !== "stop") {
    set("[data-f-gain-range]", "value", Number(item.gainDb) || 0);
    set("[data-f-gain-num]", "value", Number(item.gainDb) || 0);
    set("[data-f-fadein]", "value", Number(item.fadeIn) || 0);
    set("[data-f-fadeout]", "value", Number(item.fadeOut) || 0);
    set("[data-f-fadeshape]", "value", item.fadeShape);
    const loop = ed.querySelector("[data-f-loop]");
    if (loop && !isFocused(loop)) loop.checked = !!item.loop;
    if (waveformHandle) {
      waveformHandle.setTrim(Number(item.trimIn) || 0, Number(item.trimOut) || 0);
      updateTrimReadout(Number(item.trimIn) || 0, Number(item.trimOut) > 0 ? Number(item.trimOut) : Number(item.duration) || 0);
    }
  } else {
    set("[data-f-stoptarget]", "value", item.stopTarget);
    set("[data-f-stopmode]", "value", item.stopMode);
    const fadeSecsField = ed.querySelector("[data-fadesecs-field]");
    if (fadeSecsField) fadeSecsField.hidden = item.stopMode !== "fade";
    set("[data-f-stopfadesecs]", "value", Number(item.stopFadeSeconds) || 0);
  }
}

// ---------------------------------------------------------------- audition

// The currently selected library item, read fresh from the store (never a
// stale closure) so audition always uses the latest trim/gain/fades.
function currentItem() {
  return selectedId ? store.libraryItem(selectedId) : null;
}

function isAuditionPlaying() {
  return auditionTarget === "device" ? deviceAudition.playing : serverAuditionPlaying;
}

// Update the Play/Stop button text + style to match the live audition state.
function updatePlayButton() {
  const playBtn = els && els.editor && els.editor.querySelector("[data-play]");
  if (!playBtn) return;
  const playing = isAuditionPlaying();
  playBtn.textContent = playing ? "Stop" : "Play";
  playBtn.classList.toggle("playing", playing);
}

// Start/stop the current selection's audition on the active target. Exported
// so the global keyboard handler can bind it to the spacebar (Library tab).
export async function toggleAudition() {
  const item = currentItem();
  if (!item || item.type === "stop") return;
  if (auditionTarget === "server") {
    if (serverAuditionPlaying) {
      send("stopAudition");
      serverAuditionPlaying = false;
    } else {
      send("auditionItem", { libraryItemId: item.id });
      serverAuditionPlaying = true;
    }
  } else {
    if (deviceAudition.playing) {
      stopDeviceAudition();
    } else {
      await playDeviceAudition(item);
    }
  }
  updatePlayButton();
  updatePlayheadMarker();
}

// Apply a gain change to a live "this device" preview without restarting it.
function applyLiveDeviceGain(gainDb) {
  if (auditionTarget !== "device" || !deviceAudition.playing || !deviceAudition.gainNode) {
    return;
  }
  const lin = Math.pow(10, (Number(gainDb) || 0) / 20);
  try {
    const ctx = getAudioContext();
    deviceAudition.gainNode.gain.cancelScheduledValues(ctx.currentTime);
    deviceAudition.gainNode.gain.setValueAtTime(lin, ctx.currentTime);
  } catch { /* context unavailable */ }
}

function wireAudition(item, isStop) {
  const ed = els.editor;
  const targetBtns = ed.querySelectorAll("[data-target]");
  const playBtn = ed.querySelector("[data-play]");

  targetBtns.forEach((btn) => {
    btn.addEventListener("click", () => {
      if (btn.disabled) return;
      auditionTarget = btn.dataset.target;
      targetBtns.forEach((b) => b.classList.toggle("active", b === btn));
      updatePlayButton();
      updatePlayheadMarker();
    });
  });

  if (playBtn && !isStop) {
    playBtn.addEventListener("click", () => { toggleAudition(); });
  }

  updatePlayButton();
}

async function playDeviceAudition(item) {
  stopDeviceAudition();
  let buf;
  try {
    buf = await getAudioBuffer(item.audioHash);
  } catch (e) {
    await alertDialog("Could not decode audio for preview: " + e.message);
    return;
  }
  const ctx = getAudioContext();
  const trimIn = Math.max(0, Number(item.trimIn) || 0);
  const trimOut = item.trimOut && Number(item.trimOut) > 0 ? Number(item.trimOut) : buf.duration;
  const dur = Math.max(0, trimOut - trimIn);
  if (dur <= 0) return;

  const src = ctx.createBufferSource();
  src.buffer = buf;
  const gainNode = ctx.createGain();
  const linGain = Math.pow(10, (Number(item.gainDb) || 0) / 20);
  const fadeIn = Math.max(0, Math.min(dur, Number(item.fadeIn) || 0));
  const fadeOut = Math.max(0, Math.min(dur, Number(item.fadeOut) || 0));
  const now = ctx.currentTime;

  gainNode.gain.setValueAtTime(fadeIn > 0 ? 0 : linGain, now);
  if (fadeIn > 0) gainNode.gain.linearRampToValueAtTime(linGain, now + fadeIn);
  if (fadeOut > 0) {
    gainNode.gain.setValueAtTime(linGain, now + Math.max(fadeIn, dur - fadeOut));
    gainNode.gain.linearRampToValueAtTime(0, now + dur);
  }

  src.connect(gainNode).connect(ctx.destination);
  src.start(now, trimIn, dur);
  deviceAudition = { source: src, gainNode, playing: true, startTime: now, trimIn, trimOut };
  src.onended = () => {
    if (deviceAudition.source === src) {
      deviceAudition = { source: null, gainNode: null, playing: false, startTime: 0, trimIn: 0, trimOut: 0 };
      updatePlayButton();
      updatePlayheadMarker();
    }
  };
}

function stopDeviceAudition() {
  if (deviceAudition.source) {
    try {
      deviceAudition.source.onended = null;
      deviceAudition.source.stop();
    } catch { /* already stopped */ }
  }
  deviceAudition = { source: null, gainNode: null, playing: false, startTime: 0, trimIn: 0, trimOut: 0 };
}

// Stop whatever is auditioning (either target) and clear the playhead marker.
// Used when a trim handle is committed.
function stopAuditionForTrim() {
  if (serverAuditionPlaying) {
    send("stopAudition");
    serverAuditionPlaying = false;
  }
  serverPlayhead = null;
  stopDeviceAudition();
  updatePlayButton();
  updatePlayheadMarker();
}
