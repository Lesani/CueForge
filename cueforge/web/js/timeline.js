// CueForge compound-cue timeline editor (modal). A near-fullscreen multi-track
// editor for a LibraryItem of type "compound". The modal owns a deep clone of
// the item's timeline while open; server snapshots refresh only the status
// chip, per-clip missing marks and the palette -- never the local geometry.
// Edits are sent to the server debounced (updateTimeline); the pending send is
// flushed on close and before an explicit re-render (status-chip Retry).
//
// A client-side preview engine (timelinePreview.js) plays the timeline through
// WebAudio with a moving playhead, so the operator hears/sees edits without a
// server round-trip. No framework, no build step: plain DOM + pointer events,
// mirroring waveform.js / playing.js. All time<->px, snap and hit-zone math
// lives in the pure timelineGeometry.js so it stays testable.

import * as store from "./store.js";
import { send } from "./ws.js";
import { getAudioBuffer } from "./audioCache.js";
import { esc, formatClock, isTypingTarget } from "./util.js";
import { confirmDialog } from "./dialogs.js";
import { createTimelinePreview } from "./timelinePreview.js";
import { envAt } from "./fadeEnvelope.js";
import {
  SNAP_SECONDS, EDGE_PX,
  timeToX, xToTime, snap, snapValue, snapWithEdges,
  clipSpan, clipRect, clipZone, clipOutSeconds, chooseTickInterval,
  resolveNoOverlap,
} from "./timelineGeometry.js";

// -------------------------------------------------------------- constants
const RULER_H = 22;          // px height of the time ruler band
const LANE_H = 76;           // px height shared by head rows and lanes (keep == timeline.css)
const MIN_CLIP = 0.05;       // smallest allowed clip span (seconds)
const TAIL_SECONDS = 5;      // extra lane length past content, for drag room
const MIN_DISPLAY = 20;      // smallest displayed timeline length (seconds)
const FIT_MIN_DUR = 10;      // content length "Show all" targets when short
const DEBOUNCE_MS = 400;     // updateTimeline debounce
const DRAG_SLOP_PX = 4;      // pointer travel under which a drag counts as a click
const UNDO_CAP = 50;         // max undo snapshots retained
// Zoom (px/sec) bounds. The lower bound is dynamic (see clampZoom) so "Show all"
// always fits however long the content is; MIN_ZOOM is only the absolute floor.
const MIN_ZOOM = 0.02;       // absolute px/sec floor (very long material)
const MAX_ZOOM = 400;
const MAX_CANVAS_PX = 16000; // skip waveform draw past this (avoids alloc failure)
const AUTO_EDGE_PX = 24;     // pointer within this of the lane edge -> auto-scroll
const AUTO_STEP_PX = 12;     // scrollLeft delta per auto-scroll frame
const ARROW_SESSION_MS = 150; // arrow-nudge undo session idle timeout
// Light waveform stroke over the darkened clip body (readable on the teal).
const WAVE_COLOR = "rgba(232,240,238,0.9)";

// -------------------------------------------------------------- state
let overlay = null;          // root .tl-overlay element (null when closed)
let editingId = null;        // compound library item id being edited
let tl = null;               // local deep clone: { tracks: [...] }
let zoom = 40;               // px per second
let selectedClipId = null;   // selected clip (drives the inspector)
let selectedTrackId = null;  // track a palette-added clip lands in
let destroyed = false;

let unsub = null;            // store unsubscribe
let sendTimer = null;        // debounce handle for updateTimeline
let dirty = false;           // local edits not yet flushed to the server

let els = null;              // cached DOM refs
let drag = null;             // active pointer drag descriptor
let lastPaletteSig = null;   // gate palette rebuilds to real source changes

let preview = null;          // timelinePreview handle
let previewTime = 0;         // current playhead time (seconds)
let lastChipSig = null;      // gate chip rewrites (refreshLive fires ~15 Hz)

let undoStack = [];          // snapshots of tl BEFORE each committed change
let redoStack = [];

let trimWaveRaf = 0;         // RAF handle: live waveform redraw during a trim drag
let arrowSession = false;    // an arrow-nudge undo session is open
let arrowTimer = null;       // idle timer that ends the arrow session

// -------------------------------------------------------------- public API
export function openTimelineEditor(libraryItemId) {
  const item = store.libraryItem(libraryItemId);
  if (!item || item.type !== "compound") return;
  if (overlay) close();      // never stack two editors

  editingId = libraryItemId;
  tl = deepCloneTimeline(item.timeline);
  selectedClipId = null;
  selectedTrackId = tl.tracks.length ? tl.tracks[0].id : null;
  destroyed = false;
  dirty = false;
  lastPaletteSig = null;
  lastChipSig = null;
  previewTime = 0;
  undoStack = [];
  redoStack = [];
  arrowSession = false;
  arrowTimer = null;
  trimWaveRaf = 0;

  buildDom(item);
  unsub = store.subscribe(refreshLive);
  // Fit after layout so the lane viewport width is measured.
  requestAnimationFrame(() => { fitToContent(); renderAll(); });
}

export function isTimelineOpen() {
  return overlay != null;
}

// -------------------------------------------------------------- model helpers
function newId() {
  return "tl_" + Math.random().toString(36).slice(2, 10) + Date.now().toString(36);
}

function deepCloneTimeline(t) {
  const src = t && Array.isArray(t.tracks) ? t.tracks : [];
  return {
    tracks: src.map((tr) => ({
      id: tr.id || newId(),
      name: tr.name || "Track",
      gainDb: Number(tr.gainDb) || 0,
      mute: !!tr.mute,
      clips: (tr.clips || []).map((cl) => ({
        id: cl.id || newId(),
        itemId: cl.itemId,
        start: Number(cl.start) || 0,
        clipIn: Number(cl.clipIn) || 0,
        clipOut: cl.clipOut == null ? null : Number(cl.clipOut),
        gainDb: Number(cl.gainDb) || 0,
        fadeIn: Number(cl.fadeIn) || 0,
        fadeOut: Number(cl.fadeOut) || 0,
        fadeShape: cl.fadeShape === "equalPower" ? "equalPower" : "linear",
        effects: Array.isArray(cl.effects) ? cl.effects : [],
      })),
    })),
  };
}

function sourceItem(clip) { return store.libraryItem(clip.itemId); }
function sourceDurOf(clip) {
  const it = sourceItem(clip);
  return it ? (Number(it.duration) || 0) : 0;
}
function sourceName(clip) {
  const it = sourceItem(clip);
  return it ? it.name : "(missing)";
}
function isMissing(clip) {
  const it = sourceItem(clip);
  return !it || !it.audioHash;
}

function track(trackId) { return tl.tracks.find((t) => t.id === trackId) || null; }
function findClip(clipId) {
  for (const tr of tl.tracks) {
    const cl = tr.clips.find((c) => c.id === clipId);
    if (cl) return { track: tr, clip: cl };
  }
  return null;
}

function contentDur() {
  let max = 0;
  for (const tr of tl.tracks) {
    for (const cl of tr.clips) {
      const end = (cl.start || 0) + clipSpan(cl, sourceDurOf(cl));
      if (end > max) max = end;
    }
  }
  return max;
}
function displayDur() { return Math.max(contentDur() + TAIL_SECONDS, MIN_DISPLAY); }

// -------------------------------------------------------------- geometry glue
// A geometry "view" spanning the whole displayed timeline; laneWidth px maps
// straight to displayDur seconds so timeToX/xToTime reduce to t*zoom / x/zoom.
function gview() { return { start: 0, dur: displayDur() }; }
function laneWidth() { return Math.max(1, displayDur() * zoom); }
function t2x(t) { return timeToX(t, gview(), laneWidth()); }
function x2t(x) { return xToTime(x, gview(), laneWidth()); }

function laneViewportWidth() {
  if (!els || !els.lanes) return 800;
  return Math.max(120, els.lanes.clientWidth);
}

function fitToContent() {
  // "Show all": fit content + tail into the viewport (never below the floor).
  const dur = Math.max(contentDur() + TAIL_SECONDS, FIT_MIN_DUR);
  zoom = clampZoom(laneViewportWidth() / dur);
  updateZoomSlider();
}

// Dynamic zoom bounds: the lower bound guarantees the whole timeline can fit in
// the viewport ("Show all" always works), capped at 1.5 px/s for short content
// and floored at MIN_ZOOM for very long content.
function zoomBounds() {
  const fit = laneViewportWidth() / Math.max(contentDur() + TAIL_SECONDS, 1);
  const zMin = Math.max(MIN_ZOOM, Math.min(1.5, fit));
  return { zMin, zMax: MAX_ZOOM };
}
function clampZoom(z) {
  const { zMin, zMax } = zoomBounds();
  return Math.max(zMin, Math.min(zMax, z));
}

// Set zoom keeping the time under the viewport centre fixed, then re-render and
// sync the slider. Shared by the -/+ buttons and the slider.
function applyZoomAnchored(newZoom) {
  const lanes = els.lanes;
  const centreX = lanes.scrollLeft + lanes.clientWidth / 2;
  const anchorT = centreX / zoom;
  zoom = clampZoom(newZoom);
  renderRuler(); renderTracks();
  lanes.scrollLeft = Math.max(0, anchorT * zoom - lanes.clientWidth / 2);
  updateZoomSlider();
}
function zoomBy(factor) { applyZoomAnchored(zoom * factor); }

// Log-scale slider [0..100] <-> zoom in [zMin, zMax].
function onZoomSlider(v) {
  const { zMin, zMax } = zoomBounds();
  applyZoomAnchored(zMin * Math.pow(zMax / zMin, Math.max(0, Math.min(100, v)) / 100));
}
function updateZoomSlider() {
  if (!els || !els.zoomSlider) return;
  const { zMin, zMax } = zoomBounds();
  const v = zMax > zMin ? 100 * Math.log(zoom / zMin) / Math.log(zMax / zMin) : 0;
  els.zoomSlider.value = String(Math.max(0, Math.min(100, Math.round(v))));
}

// Snap grid used while dragging (finer of the default grid and the ruler tick).
function snapGrid() {
  const tick = chooseTickInterval(gview(), laneWidth());
  return Math.min(SNAP_SECONDS, tick);
}

// -------------------------------------------------------------- DOM build
function buildDom(item) {
  overlay = document.createElement("div");
  overlay.className = "modal-overlay tl-overlay";
  overlay.innerHTML = `
    <div class="tl-modal">
      <div class="tl-head">
        <div class="tl-transport">
          <button class="tl-play" type="button" data-tl-play title="Play (Space)"></button>
          <span class="tl-time" data-tl-time>0:00.0 / 0:00.0</span>
        </div>
        <div class="tl-title">${esc(item.name)}</div>
        <span class="tl-chip chip" data-chip></span>
        <div class="tl-actions">
          <button class="btn" type="button" data-tl-undo title="Undo (Ctrl+Z)" disabled>Undo</button>
          <button class="btn" type="button" data-tl-redo title="Redo (Ctrl+Y)" disabled>Redo</button>
          <button class="btn" type="button" data-tl-add-track title="Add a new track">Add track</button>
          <button class="btn" type="button" data-tl-zoom-out title="Zoom out">&minus;</button>
          <input class="tl-zoom-slider" type="range" min="0" max="100" value="50" data-tl-zoomslider title="Zoom" />
          <button class="btn" type="button" data-tl-zoom-in title="Zoom in">+</button>
          <button class="btn" type="button" data-tl-fit title="Fit the whole timeline">Show all</button>
          <button class="btn primary" type="button" data-tl-close title="Close the editor">Done</button>
        </div>
      </div>
      <div class="tl-body">
        <div class="tl-palette" data-palette></div>
        <div class="tl-main">
          <div class="tl-grid">
            <div class="tl-headcol">
              <div class="tl-corner" data-corner></div>
              <div class="tl-heads" data-heads></div>
            </div>
            <div class="tl-lanes" data-lanes>
              <div class="tl-inner" data-inner>
                <div class="tl-ruler" data-ruler></div>
                <div class="tl-tracks" data-tracks></div>
                <div class="tl-newtrack-hint" data-newtrack hidden>Release to add a new track</div>
                <div class="tl-playhead" data-playhead hidden></div>
              </div>
            </div>
          </div>
        </div>
      </div>
      <div class="tl-inspector" data-inspector hidden></div>
      <div class="tl-drag-tip" data-drag-tip hidden></div>
    </div>`;
  document.body.appendChild(overlay);

  els = {
    modal: overlay.querySelector(".tl-modal"),
    chip: overlay.querySelector("[data-chip]"),
    palette: overlay.querySelector("[data-palette]"),
    corner: overlay.querySelector("[data-corner]"),
    heads: overlay.querySelector("[data-heads]"),
    lanes: overlay.querySelector("[data-lanes]"),
    inner: overlay.querySelector("[data-inner]"),
    ruler: overlay.querySelector("[data-ruler]"),
    tracks: overlay.querySelector("[data-tracks]"),
    newTrackHint: overlay.querySelector("[data-newtrack]"),
    playhead: overlay.querySelector("[data-playhead]"),
    inspector: overlay.querySelector("[data-inspector]"),
    dragTip: overlay.querySelector("[data-drag-tip]"),
    play: overlay.querySelector("[data-tl-play]"),
    time: overlay.querySelector("[data-tl-time]"),
    undo: overlay.querySelector("[data-tl-undo]"),
    redo: overlay.querySelector("[data-tl-redo]"),
    zoomSlider: overlay.querySelector("[data-tl-zoomslider]"),
  };
  els.corner.style.height = RULER_H + "px";

  overlay.querySelector("[data-tl-close]").addEventListener("click", close);
  overlay.querySelector("[data-tl-add-track]").addEventListener("click", addTrack);
  overlay.querySelector("[data-tl-zoom-in]").addEventListener("click", () => zoomBy(1.6));
  overlay.querySelector("[data-tl-zoom-out]").addEventListener("click", () => zoomBy(1 / 1.6));
  overlay.querySelector("[data-tl-fit]").addEventListener("click", () => { fitToContent(); renderAll(); });
  els.zoomSlider.addEventListener("input", () => onZoomSlider(Number(els.zoomSlider.value)));
  els.play.addEventListener("click", togglePreview);
  els.undo.addEventListener("click", undo);
  els.redo.addEventListener("click", redo);

  overlay.addEventListener("click", (e) => { if (e.target === overlay) close(); });
  document.addEventListener("keydown", onKeydown, true);

  // Vertical scroll mirror (heads follow the lane viewport) + Ctrl+wheel zoom.
  els.lanes.addEventListener("scroll", () => { els.heads.scrollTop = els.lanes.scrollTop; });
  els.lanes.addEventListener("wheel", onLaneWheel, { passive: false });

  // Ruler pointer seek (click / drag) -- lanes themselves are for clip ops.
  els.ruler.addEventListener("pointerdown", onRulerPointerDown);

  // Pointer interaction is delegated on the tracks container.
  els.tracks.addEventListener("pointerdown", onLanePointerDown);

  // Palette drag-drop onto lanes (mouse/pen native DnD; touch uses click-add).
  els.lanes.addEventListener("dragover", onLaneDragOver);
  els.lanes.addEventListener("dragleave", (e) => {
    if (!e.relatedTarget || !els.lanes.contains(e.relatedTarget)) els.lanes.classList.remove("dragover");
  });
  els.lanes.addEventListener("drop", onLaneDrop);

  preview = createTimelinePreview({
    getClips: collectPreviewClips,
    getContentEnd: contentDur,
    onFrame: (t) => { previewTime = t; updatePlayhead(t); renderReadout(); followPlayhead(t); },
    onState: (p) => {
      els.play.classList.toggle("playing", p);
      els.play.title = p ? "Pause (Space)" : "Play (Space)";
    },
  });
}

// -------------------------------------------------------------- rendering
function renderAll() {
  if (destroyed) return;
  renderRuler();
  renderTracks();
  renderPalette();
  renderInspector();
  renderReadout();
  updateHistButtons();
  updateZoomSlider();
  refreshLive();
}

function renderRuler() {
  const ruler = els.ruler;
  // Empty timeline: the lanes collapse to the viewport (renderEmptyState forces
  // .tl-inner to 100%), so the ruler must match -- a fixed px width here would
  // leave a phantom horizontal scrollbar (spec C8).
  if (!tl.tracks.length) {
    ruler.style.width = "100%";
    ruler.style.height = RULER_H + "px";
    ruler.innerHTML = "";
    return;
  }
  const w = laneWidth();
  ruler.style.width = w + "px";
  const iv = chooseTickInterval(gview(), w);
  const end = displayDur();
  let html = "";
  for (let t = 0; t <= end + 1e-9; t += iv) {
    const x = t2x(t);
    html += `<div class="tl-tick" style="left:${x.toFixed(1)}px"><span>${esc(fmtTick(t, iv))}</span></div>`;
  }
  ruler.innerHTML = html;
  ruler.style.height = RULER_H + "px";
}

function fmtTick(t, iv) {
  if (iv < 1) return t.toFixed(2) + "s";
  const m = Math.floor(t / 60), s = Math.floor(t % 60);
  return m + ":" + String(s).padStart(2, "0");
}

// "0:03.2" -- like formatClock but with a tenths digit for the transport readout.
function fmtHMSt(t) {
  t = Math.max(0, t);
  const m = Math.floor(t / 60), s = Math.floor(t % 60), d = Math.floor((t * 10) % 10);
  return `${m}:${String(s).padStart(2, "0")}.${d}`;
}

function renderReadout() {
  if (!els || !els.time) return;
  els.time.textContent = `${fmtHMSt(previewTime)} / ${fmtHMSt(contentDur())}`;
}

function renderTracks() {
  const w = laneWidth();
  if (!tl.tracks.length) { renderEmptyState(); return; }
  let heads = "", lanes = "";
  for (const tr of tl.tracks) {
    const sel = tr.id === selectedTrackId ? " selected" : "";
    heads += `
      <div class="tl-head-row${sel}" data-track="${esc(tr.id)}" style="height:${LANE_H}px">
        <input class="tl-track-name" data-track-name value="${esc(tr.name)}" title="Track name" />
        <div class="tl-track-ctl">
          <label class="tl-track-gain-lbl">Volume
            <input type="number" class="tl-track-gain" data-track-gain step="0.5" value="${tr.gainDb}" title="Track volume in dB" />
          </label>
          <label class="tl-track-mute-lbl">
            <input type="checkbox" class="tl-track-mute" data-track-mute ${tr.mute ? "checked" : ""} /> Mute
          </label>
          <button type="button" class="tl-track-del" data-track-del title="Delete track">&times;</button>
        </div>
      </div>`;
    const muted = tr.mute ? " muted" : "";
    lanes += `
      <div class="tl-lane${sel}${muted}" data-lane="${esc(tr.id)}" style="width:${w}px;height:${LANE_H}px">
        ${tr.clips.map((cl) => clipHtml(tr, cl)).join("")}
      </div>`;
  }
  els.heads.innerHTML = heads;
  els.tracks.innerHTML = lanes;
  els.inner.style.width = w + "px";
  wireTrackHeads();
  drawAllClipWaves();
  updatePlayhead();
}

// Friendly onboarding shown when there are no tracks. Clears BOTH stacks and
// resets the inner width so the message centres (no stale head rows linger).
function renderEmptyState() {
  els.heads.innerHTML = "";
  els.tracks.innerHTML = `
    <div class="tl-onboard">
      <div class="tl-onboard-title">Build your layered cue here</div>
      <div class="tl-onboard-body">
        Drag a sound from the list on the left onto the timeline, or click a sound
        to drop it in. Use <b>Add track</b> for a separate layer.
      </div>
    </div>`;
  els.inner.style.width = "100%";
  updatePlayhead();
}

function clipHtml(tr, cl) {
  const sourceDur = sourceDurOf(cl);
  const { x, w } = clipRect(cl, sourceDur, gview(), laneWidth());
  const span = clipSpan(cl, sourceDur);
  const fiPx = span > 0 ? Math.min(w, (cl.fadeIn / span) * w) : 0;
  const foPx = span > 0 ? Math.min(w, (cl.fadeOut / span) * w) : 0;
  const missing = isMissing(cl) ? " missing" : "";
  const sel = cl.id === selectedClipId ? " selected" : "";
  // Amendment 1: inline the fade dots' left/right at build time with the SAME
  // formula layoutClipEl uses, so a fresh renderTracks() places them at the fade
  // line immediately (not the CSS-fallback corners until the next layoutClipEl).
  const dlLeft = Math.max(0, fiPx - 5).toFixed(1);
  const drRight = Math.max(0, foPx - 5).toFixed(1);
  return `
    <div class="tl-clip${missing}${sel}" data-clip="${esc(cl.id)}"
         title="Drag to move &middot; drag edges to trim &middot; drag top corners to fade"
         style="left:${x.toFixed(1)}px;width:${Math.max(2, w).toFixed(1)}px">
      <canvas class="tl-clip-wave"></canvas>
      <div class="tl-grip tl-grip-l" title="Trim start"></div>
      <div class="tl-grip tl-grip-r" title="Trim end"></div>
      <div class="tl-fadedot tl-fadedot-l" title="Fade in" style="left:${dlLeft}px"></div>
      <div class="tl-fadedot tl-fadedot-r" title="Fade out" style="right:${drRight}px"></div>
      <div class="tl-clip-label">${esc(sourceName(cl))}</div>
    </div>`;
}

function drawAllClipWaves() {
  if (!els) return;
  els.tracks.querySelectorAll(".tl-clip[data-clip]").forEach((clipEl) => {
    const found = findClip(clipEl.dataset.clip);
    if (found) drawClipWave(clipEl.querySelector(".tl-clip-wave"), found.clip);
  });
}

// Mirror waveform.js computePeaks for the clip's [clipIn, clipOut] source range,
// mapped across the clip's pixel width. Skips gracefully on missing/undecodable
// sources or a clip too wide to back with a canvas.
async function drawClipWave(canvas, clip) {
  const src = sourceItem(clip);
  if (!canvas || !src || !src.audioHash) return;
  let buf;
  try { buf = await getAudioBuffer(src.audioHash); }
  catch { return; }
  if (destroyed || !canvas.isConnected) return;

  const w = Math.max(1, Math.floor(canvas.clientWidth));
  const h = Math.max(1, Math.floor(canvas.clientHeight));
  const dpr = window.devicePixelRatio || 1;
  if (w * dpr > MAX_CANVAS_PX) return;   // absurd zoom/length -> leave blank
  canvas.width = w * dpr;
  canvas.height = h * dpr;
  const g = canvas.getContext("2d");
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, w, h);

  const ch0 = buf.getChannelData(0);
  const sr = buf.sampleRate;
  const inS = Math.max(0, clip.clipIn || 0);
  const outS = clip.clipOut == null ? buf.duration : clip.clipOut;
  const s0 = Math.max(0, Math.floor(inS * sr));
  const s1 = Math.min(ch0.length, Math.ceil(outS * sr));
  const sampleSpan = Math.max(1, s1 - s0);
  const perPixel = sampleSpan / w;
  // Clip-local seconds across the pixel width, for the fade envelope.
  const span = clipSpan(clip, buf.duration);
  const mid = h / 2, amp = (h / 2) * 0.84;
  g.strokeStyle = WAVE_COLOR;
  g.lineWidth = 1;
  g.beginPath();
  for (let x = 0; x < w; x++) {
    const s = s0 + Math.floor(x * perPixel);
    const e = Math.min(s1, Math.max(s + 1, s0 + Math.floor((x + 1) * perPixel)));
    let mn = 0, mx = 0;
    for (let i = s; i < e; i++) { const v = ch0[i]; if (v < mn) mn = v; if (v > mx) mx = v; }
    const env = envAt((x / w) * span, clip.fadeIn, clip.fadeOut, span, clip.fadeShape);
    g.moveTo(x + 0.5, mid - mx * amp * env);
    g.lineTo(x + 0.5, mid - mn * amp * env + 0.5);
  }
  g.stroke();
  // Thin fade contour tracing the upper envelope where env<1 (no gradient).
  g.strokeStyle = "rgba(255,255,255,0.5)";
  g.beginPath();
  for (let x = 0; x < w; x++) {
    const env = envAt((x / w) * span, clip.fadeIn, clip.fadeOut, span, clip.fadeShape);
    const y = mid - amp * env;
    if (x === 0) g.moveTo(x + 0.5, y); else g.lineTo(x + 0.5, y);
  }
  g.stroke();
}

function renderPalette() {
  const show = store.show();
  const all = show && show.library ? Object.values(show.library) : [];
  const items = all
    .filter((it) => it.audioHash && it.type !== "compound" && it.id !== editingId)
    .sort((a, b) => a.name.localeCompare(b.name));
  // Gate the rebuild: the store fires ~15 Hz, but the source list rarely
  // changes, and an unconditional innerHTML swap would fight an in-flight
  // palette drag.
  const sig = items.map((it) => it.id + it.name + it.audioHash + (it.duration || 0)).join("|");
  if (sig === lastPaletteSig) return;
  lastPaletteSig = sig;
  let html = `<div class="tl-palette-title">Sounds</div>
    <div class="tl-palette-hint">Drag onto the timeline, or click to add.</div>`;
  if (!items.length) {
    html += `<div class="tl-palette-empty">No audio items available. Import audio in the Library tab first.</div>`;
  } else {
    html += items.map((it) => `
      <button type="button" class="tl-pal-item" draggable="true"
              data-pal="${esc(it.id)}" title="Click or drag onto a track">
        <span class="tl-pal-name">${esc(it.name)}</span>
        <span class="tl-pal-dur">${esc(it.duration ? formatClock(Number(it.duration)) : "--:--")}</span>
      </button>`).join("");
  }
  els.palette.innerHTML = html;
  els.palette.querySelectorAll("[data-pal]").forEach((btn) => {
    btn.addEventListener("click", () => addClipFromSource(btn.dataset.pal, null));
    btn.addEventListener("dragstart", (e) => {
      e.dataTransfer.setData("text/plain", "tl-src:" + btn.dataset.pal);
      e.dataTransfer.effectAllowed = "copy";
    });
  });
}

function renderInspector() {
  const insp = els.inspector;
  const found = selectedClipId ? findClip(selectedClipId) : null;
  if (!found) { insp.hidden = true; insp.innerHTML = ""; return; }
  const cl = found.clip;
  const sourceDur = sourceDurOf(cl);
  const outS = clipOutSeconds(cl, sourceDur);
  insp.hidden = false;
  insp.innerHTML = `
    <div class="tl-insp-title">${esc(sourceName(cl))}</div>
    <label class="tl-insp-field">Start (seconds)
      <input type="number" min="0" step="0.1" data-insp-start value="${+(cl.start || 0).toFixed(2)}" />
    </label>
    <label class="tl-insp-field">Volume (dB)
      <input type="number" step="0.5" data-insp-gain value="${+(cl.gainDb || 0).toFixed(1)}" />
    </label>
    <label class="tl-insp-field">Fade in (seconds)
      <input type="number" min="0" step="0.05" data-insp-fadein value="${+(cl.fadeIn || 0).toFixed(2)}" />
    </label>
    <label class="tl-insp-field">Fade out (seconds)
      <input type="number" min="0" step="0.05" data-insp-fadeout value="${+(cl.fadeOut || 0).toFixed(2)}" />
    </label>
    <label class="tl-insp-field">Fade curve
      <select data-insp-fadeshape>
        <option value="equalPower"${cl.fadeShape === "equalPower" ? " selected" : ""}>Smooth</option>
        <option value="linear"${cl.fadeShape === "linear" ? " selected" : ""}>Straight</option>
      </select>
    </label>
    <div class="tl-insp-readout">Using ${esc(formatClock(cl.clipIn))}&ndash;${esc(formatClock(outS))} of the sound</div>
    <button type="button" class="btn danger tl-insp-del" data-insp-del>Delete sound</button>`;

  const st = insp.querySelector("[data-insp-start]");
  st.addEventListener("input", () => {
    const span = clipSpan(cl, sourceDurOf(cl));
    const desired = resolveNoOverlap(trackClipsForResolve(found.track), cl.id, Math.max(0, Number(st.value) || 0), span);
    cl.start = Math.max(0, desired);
    mutate(); renderTracks(); rebuildPreview();
  });
  historyField(st);
  const g = insp.querySelector("[data-insp-gain]");
  g.addEventListener("input", () => { cl.gainDb = Number(g.value) || 0; mutate(); rebuildPreview(); });
  historyField(g);
  const fi = insp.querySelector("[data-insp-fadein]");
  fi.addEventListener("input", () => {
    cl.fadeIn = clampFade(Number(fi.value) || 0, cl, cl.fadeOut); mutate(); redrawClip(cl.id); rebuildPreview();
  });
  historyField(fi);
  const fo = insp.querySelector("[data-insp-fadeout]");
  fo.addEventListener("input", () => {
    cl.fadeOut = clampFade(Number(fo.value) || 0, cl, cl.fadeIn); mutate(); redrawClip(cl.id); rebuildPreview();
  });
  historyField(fo);
  const fs = insp.querySelector("[data-insp-fadeshape]");
  fs.addEventListener("change", () => { cl.fadeShape = fs.value; mutate(); rebuildPreview(); });
  historyField(fs);
  insp.querySelector("[data-insp-del]").addEventListener("click", () => deleteClip(cl.id));
}

// Clamp a fade to [0, span - other] so fade-in + fade-out can never cross
// (spec B9). `other` is the opposing fade's current length.
function clampFade(v, clip, other = 0) {
  const span = clipSpan(clip, sourceDurOf(clip));
  return Math.max(0, Math.min(Math.max(0, span - (other || 0)), v));
}

// Live refresh from a server snapshot: status chip, palette, per-clip missing
// marks. NEVER touches clip positions/sizes or the playhead/scroll -- the modal
// owns geometry while open, so a 15 Hz store tick stays layout-safe.
function refreshLive() {
  if (destroyed || !els) return;
  const item = store.libraryItem(editingId);
  if (!item) { close(); return; }   // compound deleted elsewhere

  updateChip();
  // Play is meaningless with no playable content -> disable it (cheap + idempotent).
  if (els.play) els.play.disabled = contentDur() <= 0;

  // Per-clip missing marks + labels (positions untouched).
  els.tracks.querySelectorAll(".tl-clip[data-clip]").forEach((clipEl) => {
    const found = findClip(clipEl.dataset.clip);
    if (!found) return;
    clipEl.classList.toggle("missing", isMissing(found.clip));
    const lbl = clipEl.querySelector(".tl-clip-label");
    if (lbl) lbl.textContent = sourceName(found.clip);
  });

  renderPalette();
}

// Plain-language status chip (DOM-only; never re-lays-out lanes). Gated on an
// actual content change so the ~15 Hz store tick never churns the DOM (which
// would also restart the spinner animation every frame).
function updateChip() {
  const item = store.libraryItem(editingId);
  if (!item || !els.chip) return;
  const hasClips = tl.tracks.some((tr) => tr.clips.length);
  const st = item.renderState || "";
  let cls = "tl-chip", html;
  if (!hasClips) { cls += " empty"; html = "Add sounds to begin"; }
  else if (st === "pending" || st === "rendering") { cls += " busy"; html = `<span class="tl-spin"></span>Preparing&hellip;`; }
  else if (st === "error") { cls += " error"; html = `Problem preparing this cue <button class="btn tl-retry" data-chip-retry>Retry</button>`; }
  else if (st === "ready" || item.audioHash) { cls += " ready"; html = "Ready &middot; " + formatClock(Number(item.duration) || 0); }
  else { cls += " busy"; html = `<span class="tl-spin"></span>Preparing&hellip;`; }
  const sig = cls + "|" + html;
  if (sig === lastChipSig) return;
  lastChipSig = sig;
  els.chip.className = cls;
  els.chip.innerHTML = html;
  const rt = els.chip.querySelector("[data-chip-retry]");
  if (rt) rt.addEventListener("click", () => { flushSend(); send("renderCompound", { itemId: editingId }); });
}

// Repaint one clip element in place (fade widths, waveform) after a param edit.
function redrawClip(clipId) {
  const clipEl = els.tracks.querySelector(`.tl-clip[data-clip="${cssEsc(clipId)}"]`);
  const found = findClip(clipId);
  if (!clipEl || !found) return;
  layoutClipEl(clipEl, found.clip);
  drawClipWave(clipEl.querySelector(".tl-clip-wave"), found.clip);
}

// Apply a clip's model geometry to its element (left/width + fade dot positions).
function layoutClipEl(clipEl, cl) {
  const sourceDur = sourceDurOf(cl);
  const { x, w } = clipRect(cl, sourceDur, gview(), laneWidth());
  const span = clipSpan(cl, sourceDur);
  clipEl.style.left = x.toFixed(1) + "px";
  clipEl.style.width = Math.max(2, w).toFixed(1) + "px";
  const fiPx = span > 0 ? Math.min(w, (cl.fadeIn / span) * w) : 0;
  const foPx = span > 0 ? Math.min(w, (cl.fadeOut / span) * w) : 0;
  const dl = clipEl.querySelector(".tl-fadedot-l");
  const dr = clipEl.querySelector(".tl-fadedot-r");
  if (dl) dl.style.left  = Math.max(0, fiPx - 5).toFixed(1) + "px";   // center the 10px dot
  if (dr) dr.style.right = Math.max(0, foPx - 5).toFixed(1) + "px";
}

function cssEsc(s) {
  return window.CSS && CSS.escape ? CSS.escape(s) : String(s).replace(/"/g, '\\"');
}

// -------------------------------------------------------------- playhead / transport
function updatePlayhead(t = previewTime) {
  if (!els || !els.playhead) return;
  els.playhead.hidden = false;
  els.playhead.style.left = t2x(t).toFixed(1) + "px";
}

// Gentle follow: only scroll when the playhead leaves the viewport (never from
// a store tick -- this is only called on a preview frame).
function followPlayhead(t) {
  if (!els || !els.lanes) return;
  const x = t2x(t);
  const left = els.lanes.scrollLeft;
  const right = left + els.lanes.clientWidth;
  if (x < left || x > right - 40) {
    els.lanes.scrollLeft = Math.max(0, x - els.lanes.clientWidth * 0.5);
  }
}

function togglePreview() {
  if (contentDur() <= 0) return;   // nothing to play (button is also disabled)
  const t = preview.currentTime();
  preview.toggle(t >= contentDur() - 1e-3 ? 0 : t);
}

// Restart the WebAudio schedule at the current playhead after an edit, but only
// while playing (keeps live edits audible without a stop/start).
function rebuildPreview() {
  if (preview && preview.isPlaying()) preview.rebuild();
}

// Build plain PreviewClip descriptors for the preview engine: clip+track dB
// summed, muted tracks and missing/zero-span clips skipped.
function collectPreviewClips() {
  const out = [];
  for (const tr of tl.tracks) {
    if (tr.mute) continue;
    for (const cl of tr.clips) {
      const src = sourceItem(cl);
      if (!src || !src.audioHash) continue;              // missing -> silently skipped
      const span = clipSpan(cl, sourceDurOf(cl));
      if (span <= 0) continue;
      out.push({
        hash: src.audioHash, start: cl.start || 0, span, clipIn: cl.clipIn || 0,
        gainDb: (cl.gainDb || 0) + (tr.gainDb || 0),
        fadeIn: cl.fadeIn || 0, fadeOut: cl.fadeOut || 0, fadeShape: cl.fadeShape,
      });
    }
  }
  return out;
}

// -------------------------------------------------------------- track ops
function wireTrackHeads() {
  els.heads.querySelectorAll(".tl-head-row").forEach((row) => {
    const trackId = row.dataset.track;
    const tr = track(trackId);
    if (!tr) return;
    row.addEventListener("pointerdown", (e) => {
      if (e.target.closest("input,button,select,label")) return;
      selectTrack(trackId);
    });
    const name = row.querySelector("[data-track-name]");
    name.addEventListener("input", () => { tr.name = name.value; mutate(); });
    historyField(name);
    const gain = row.querySelector("[data-track-gain]");
    gain.addEventListener("input", () => { tr.gainDb = Number(gain.value) || 0; mutate(); rebuildPreview(); });
    historyField(gain);
    const mute = row.querySelector("[data-track-mute]");
    mute.addEventListener("change", () => {
      tr.mute = mute.checked;
      const lane = els.tracks.querySelector(`.tl-lane[data-lane="${cssEsc(tr.id)}"]`);
      if (lane) lane.classList.toggle("muted", tr.mute);
      mutate(); rebuildPreview();
    });
    historyField(mute);
    row.querySelector("[data-track-del]").addEventListener("click", () => deleteTrack(trackId));
  });
}

function addTrack() {
  pushSnapshot(deepCloneTimeline(tl));
  const tr = { id: newId(), name: "Track " + (tl.tracks.length + 1), gainDb: 0, mute: false, clips: [] };
  tl.tracks.push(tr);
  selectedTrackId = tr.id;
  mutate();
  renderTracks();
  updateChip();
}

async function deleteTrack(trackId) {
  const tr = track(trackId);
  if (tr && tr.clips.length) {
    const ok = await confirmDialog(
      `Delete track and its ${tr.clips.length} sound${tr.clips.length === 1 ? "" : "s"} from this timeline?`,
      { title: "Delete track", okLabel: "Delete", danger: true });
    if (!ok) return;
  }
  pushSnapshot(deepCloneTimeline(tl));
  tl.tracks = tl.tracks.filter((t) => t.id !== trackId);
  if (selectedTrackId === trackId) selectedTrackId = tl.tracks.length ? tl.tracks[0].id : null;
  const sel = selectedClipId && findClip(selectedClipId);
  if (!sel) selectedClipId = null;
  mutate();
  renderRuler(); renderTracks(); renderInspector(); updateChip();
  rebuildPreview();
}

function selectTrack(trackId) {
  selectedTrackId = trackId;
  els.heads.querySelectorAll(".tl-head-row").forEach((row) =>
    row.classList.toggle("selected", row.dataset.track === trackId));
  els.tracks.querySelectorAll(".tl-lane").forEach((lane) =>
    lane.classList.toggle("selected", lane.dataset.lane === trackId));
}

// End (seconds) of a track's last clip -- where a click-added sound lands.
function trackContentEnd(tr) {
  let end = 0;
  for (const cl of tr.clips) end = Math.max(end, (cl.start || 0) + clipSpan(cl, sourceDurOf(cl)));
  return end;
}

// First start >= desired where a clip of `span` seconds fits without covering
// an existing clip on the track. New sounds must never land hidden under
// another one; deliberate overlaps (crossfades) stay possible by dragging.
function nonOverlappingStart(tr, desired, span) {
  const clips = [...tr.clips].sort((a, b) => (a.start || 0) - (b.start || 0));
  let t = desired;
  for (const cl of clips) {
    const s = cl.start || 0;
    const e = s + clipSpan(cl, sourceDurOf(cl));
    if (t < e && t + span > s) t = e;    // overlaps -> continue after this clip
  }
  return t;
}

// Append a clip referencing `srcId` to a track (or the selected/first track).
// Click-add (no startT) appends after the track's content; a drop keeps its
// position but is nudged right out of any overlap (no re-snap after the nudge
// -- the nudged value is an exact butt-join).
function addClipFromSource(srcId, trackId, startT, opts = {}) {
  const src = store.libraryItem(srcId);
  if (!src || src.type === "compound") return;
  pushSnapshot(deepCloneTimeline(tl));
  let tr;
  if (opts.newTrack) {
    // Dropped below all lanes -> a brand new track holds the clip. Single
    // snapshot (above) covers both the track creation and the clip add.
    tr = { id: newId(), name: "Track " + (tl.tracks.length + 1), gainDb: 0, mute: false, clips: [] };
    tl.tracks.push(tr);
  } else {
    tr = trackId ? track(trackId) : track(selectedTrackId) || tl.tracks[0];
    if (!tr) { tr = { id: newId(), name: "Track 1", gainDb: 0, mute: false, clips: [] }; tl.tracks.push(tr); }
  }
  const span = Math.max(0, Number(src.duration) || 0);
  let start;
  if (startT != null) {
    // Explicit position (drop): nearest free slot that fits, no overlap.
    const desired = Math.max(0, snap(startT, snapGrid()));
    start = resolveNoOverlap(trackClipsForResolve(tr), null, desired, span);
  } else {
    // Click-append: push right past the track's existing content.
    start = nonOverlappingStart(tr, trackContentEnd(tr), span);
  }
  const cl = {
    id: newId(), itemId: srcId, start, clipIn: 0, clipOut: null,
    gainDb: 0, fadeIn: 0, fadeOut: 0, fadeShape: "linear", effects: [],
  };
  tr.clips.push(cl);
  selectedClipId = cl.id;
  selectedTrackId = tr.id;
  mutate();
  renderRuler(); renderTracks(); renderInspector(); updateChip();
  rebuildPreview();
}

function deleteClip(clipId) {
  pushSnapshot(deepCloneTimeline(tl));
  for (const tr of tl.tracks) {
    const i = tr.clips.findIndex((c) => c.id === clipId);
    if (i >= 0) { tr.clips.splice(i, 1); break; }
  }
  if (selectedClipId === clipId) selectedClipId = null;
  mutate();
  renderRuler(); renderTracks(); renderInspector(); updateChip();
  rebuildPreview();
}

// Keyboard nudge of the selected clip by one grid step (Shift = grid/10).
// Overlap-resolved per A2. One undo snapshot per continuous key session: the
// snapshot is taken on the first keydown and the session ends ARROW_SESSION_MS
// after the last arrow keydown (or on any other key -- see onKeydown).
function nudgeSelectedClip(dir, fine) {
  const found = findClip(selectedClipId);
  if (!found) return;
  const { track: tr, clip: cl } = found;
  if (!arrowSession) { pushSnapshot(deepCloneTimeline(tl)); arrowSession = true; }
  clearTimeout(arrowTimer);
  arrowTimer = setTimeout(endArrowSession, ARROW_SESSION_MS);
  const step = (fine ? snapGrid() / 10 : snapGrid());
  const span = clipSpan(cl, sourceDurOf(cl));
  const desired = Math.max(0, (cl.start || 0) + dir * step);
  cl.start = Math.max(0, resolveNoOverlap(trackClipsForResolve(tr), cl.id, desired, span));
  mutate();
  redrawClip(cl.id);       // layoutClipEl + waveform, no full re-render (keeps key repeat smooth)
  rebuildPreview();
}
function endArrowSession() {
  arrowSession = false;
  if (arrowTimer) { clearTimeout(arrowTimer); arrowTimer = null; }
}

// -------------------------------------------------------------- interactions
function onLanePointerDown(e) {
  const clipEl = e.target.closest(".tl-clip[data-clip]");
  if (!clipEl) {
    // Empty lane space -> seek the playhead there + deselect (spec B7).
    const laneEl = e.target.closest(".tl-lane[data-lane]");
    if (laneEl) onLaneEmptyClick(e, laneEl);
    return;
  }
  const found = findClip(clipEl.dataset.clip);
  if (!found) return;
  const cl = found.clip;
  const laneEl = clipEl.parentElement;
  const laneRect = laneEl.getBoundingClientRect();
  const clipRectDom = clipEl.getBoundingClientRect();
  const localX = e.clientX - clipRectDom.left;
  const localY = e.clientY - clipRectDom.top;
  const w = clipRectDom.width;
  const span = clipSpan(cl, sourceDurOf(cl));
  const fiPx = span > 0 ? Math.min(w, (cl.fadeIn / span) * w) : 0;
  const foPx = span > 0 ? Math.min(w, (cl.fadeOut / span) * w) : 0;
  const zone = clipZone(localX, localY, w, fiPx, foPx);

  const pointerT = x2t(e.clientX - laneRect.left);
  drag = {
    clipEl, laneEl, clip: cl, track: found.track, zone,
    pointerId: e.pointerId, laneRect,
    startClientX: e.clientX, startClientY: e.clientY, moved: false,
    lastClientX: e.clientX, lastClientY: e.clientY,
    grabOffset: pointerT - (cl.start || 0),
    orig: { start: cl.start, clipIn: cl.clipIn, clipOut: cl.clipOut, fadeIn: cl.fadeIn, fadeOut: cl.fadeOut },
    pre: deepCloneTimeline(tl),
    autoRaf: 0, newTrackPending: false,
  };
  clipEl.classList.add("dragging");
  try { clipEl.setPointerCapture(e.pointerId); } catch { /* ignore */ }
  clipEl.addEventListener("pointermove", onLanePointerMove);
  clipEl.addEventListener("pointerup", onLanePointerUp);
  clipEl.addEventListener("pointercancel", onLanePointerUp);
  e.preventDefault();
}

// Click on empty lane space: seek the playhead (mirrors the ruler) and drop the
// current clip selection. Also makes that lane the active track for palette-add.
function onLaneEmptyClick(e, laneEl) {
  const laneRect = laneEl.getBoundingClientRect();
  const t = Math.max(0, x2t(e.clientX - laneRect.left));
  previewTime = t; preview.seek(t); updatePlayhead(t); renderReadout();
  if (selectedClipId) {
    selectedClipId = null;
    els.tracks.querySelectorAll(".tl-clip.selected").forEach((c) => c.classList.remove("selected"));
    renderInspector();
  }
  selectTrack(laneEl.dataset.lane);
}

function onLanePointerMove(e) {
  if (!drag || e.pointerId !== drag.pointerId) return;
  if (!drag.moved &&
      Math.abs(e.clientX - drag.startClientX) < DRAG_SLOP_PX &&
      Math.abs(e.clientY - drag.startClientY) < DRAG_SLOP_PX) {
    return;                       // still within click slop
  }
  drag.moved = true;
  drag.lastClientX = e.clientX;
  drag.lastClientY = e.clientY;

  // A1: a body drag can cross tracks live (or arm the "new track" hint). This
  // may re-render lanes and re-acquire drag.clipEl/laneEl -- do it first.
  if (drag.zone === "body") updateDragTarget(e.clientY);

  // Audit #12: the lane can shift under scroll/zoom -> always refresh its rect
  // before mapping the pointer to a time.
  drag.laneRect = drag.laneEl.getBoundingClientRect();
  applyDragAt(e.clientX);
  showDragTip(e.clientX, e.clientY, dragTipText(drag.zone, drag.clip, sourceDurOf(drag.clip)));
  maybeAutoScroll();
  e.preventDefault();
}

// The per-zone position math, isolated so the auto-scroll RAF can re-run it each
// frame with the pointer held still (spec A3). Reads drag.laneRect (already
// refreshed by the caller) and the pointer's current clientX.
function applyDragAt(clientX) {
  const cl = drag.clip;
  const sourceDur = sourceDurOf(cl);
  const pointerT = x2t(clientX - drag.laneRect.left);
  const grid = snapGrid();
  const thr = EDGE_PX / zoom;
  const cand = otherClipEdges(cl.id);

  if (drag.zone === "body") {
    // Snap whichever of the clip's own edges (start OR end) is nearest to a
    // neighbour edge; grid applies only when neither is close (butt-joins in
    // both drag directions).
    const raw = Math.max(0, pointerT - drag.grabOffset);
    const span = clipSpan(cl, sourceDur);
    const s1 = snapValue(raw, cand, thr);
    const e1 = snapValue(raw + span, cand, thr);
    const sHit = s1 !== raw, eHit = e1 !== raw + span;
    let start;
    if (sHit && (!eHit || Math.abs(s1 - raw) <= Math.abs(e1 - (raw + span)))) {
      start = s1;
    } else if (eHit) {
      start = e1 - span;
    } else {
      start = snap(raw, grid);
    }
    // A2: never overlap another clip on the destination track.
    start = resolveNoOverlap(trackClipsForResolve(drag.track), cl.id, Math.max(0, start), span);
    cl.start = Math.max(0, start);
  } else if (drag.zone === "trimStart") {
    const effOut = clipOutSeconds(drag.orig, sourceDur);
    const desired = snapWithEdges(pointerT, grid, cand, thr);
    let delta = desired - drag.orig.start;
    const lo = Math.max(-drag.orig.clipIn, -drag.orig.start);
    const hi = effOut - MIN_CLIP - drag.orig.clipIn;
    delta = Math.max(lo, Math.min(hi, delta));
    // A2: the left edge cannot cross the previous clip's end on this track.
    const prevEnd = prevClipEnd(drag.track, cl.id, drag.orig.start);
    if (drag.orig.start + delta < prevEnd) {
      delta = Math.max(lo, Math.min(hi, prevEnd - drag.orig.start));
    }
    cl.start = drag.orig.start + delta;
    cl.clipIn = drag.orig.clipIn + delta;
  } else if (drag.zone === "trimEnd") {
    // A2: the right edge cannot cross the next clip's start on this track.
    const nextStart = nextClipStart(drag.track, cl.id, cl.start);
    const rightT = Math.min(snapWithEdges(pointerT, grid, cand, thr), nextStart);
    let newSpan = rightT - cl.start;
    let newOut = cl.clipIn + newSpan;
    const maxOut = sourceDur > 0 ? sourceDur : newOut;
    newOut = Math.max(cl.clipIn + MIN_CLIP, Math.min(maxOut, newOut));
    cl.clipOut = (sourceDur > 0 && newOut >= sourceDur - 1e-4) ? null : newOut;
  } else if (drag.zone === "fadeIn") {
    const span = clipSpan(cl, sourceDur);
    cl.fadeIn = Math.max(0, Math.min(span - (cl.fadeOut || 0), pointerT - cl.start));
  } else if (drag.zone === "fadeOut") {
    const span = clipSpan(cl, sourceDur);
    cl.fadeOut = Math.max(0, Math.min(span - (cl.fadeIn || 0), (cl.start + span) - pointerT));
  }
  layoutClipEl(drag.clipEl, cl);
  // B3b: keep the waveform honest while trimming OR fading (RAF-coalesced) --
  // fade drags reshape the envelope-scaled wave + contour, not just the dots.
  if (drag.zone === "trimStart" || drag.zone === "trimEnd" ||
      drag.zone === "fadeIn" || drag.zone === "fadeOut") scheduleTrimWave();
}

// Plain [{id, start, span}] view of a track's clips for resolveNoOverlap.
function trackClipsForResolve(tr) {
  return tr.clips.map((c) => ({ id: c.id, start: c.start || 0, span: clipSpan(c, sourceDurOf(c)) }));
}

// Greatest end time among a track's other clips that sit left of refStart.
function prevClipEnd(tr, excludeId, refStart) {
  let end = 0;
  for (const c of tr.clips) {
    if (c.id === excludeId) continue;
    const e = (c.start || 0) + clipSpan(c, sourceDurOf(c));
    if (e <= refStart + 1e-6 && e > end) end = e;
  }
  return end;
}

// Smallest start among a track's other clips at or right of refStart (Infinity
// if none).
function nextClipStart(tr, excludeId, refStart) {
  let best = Infinity;
  for (const c of tr.clips) {
    if (c.id === excludeId) continue;
    const s = c.start || 0;
    if (s >= refStart - 1e-6 && s < best) best = s;
  }
  return best;
}

// -------------------------------------------------------------- drag: cross-track + new track
// Resolve which lane (or "new track" zone) the pointer's Y is over.
function resolveTargetLane(clientY) {
  const lanes = els.tracks.querySelectorAll(".tl-lane[data-lane]");
  if (!lanes.length) return { newTrack: true };
  let first = null, last = null;
  for (const lane of lanes) {
    const r = lane.getBoundingClientRect();
    if (!first) first = { lane, r };
    last = { lane, r };
    if (clientY >= r.top && clientY < r.bottom) return { laneEl: lane, trackId: lane.dataset.lane };
  }
  if (clientY < first.r.top) return { laneEl: first.lane, trackId: first.lane.dataset.lane };
  if (clientY >= last.r.bottom) return { newTrack: true };   // below the last lane
  return { laneEl: last.lane, trackId: last.lane.dataset.lane };
}

// During a body drag: move the clip to the lane under the pointer (live), or arm
// the "new track on release" hint when below all lanes.
function updateDragTarget(clientY) {
  const t = resolveTargetLane(clientY);
  if (t.newTrack) { drag.newTrackPending = true; showNewTrackHint(true); return; }
  drag.newTrackPending = false; showNewTrackHint(false);
  if (t.trackId && t.trackId !== drag.track.id) reparentDragTo(t.trackId);
}

// Live re-parent of the dragged clip into another track: mutate the model, re-
// render lanes, then re-acquire the fresh clip element and move the pointer
// capture + listeners onto it (the old element was destroyed by renderTracks).
function reparentDragTo(newTrackId) {
  const cl = drag.clip;
  const idx = drag.track.clips.indexOf(cl);
  if (idx >= 0) drag.track.clips.splice(idx, 1);
  const newTr = track(newTrackId);
  if (!newTr) { drag.track.clips.push(cl); return; }   // defensive: undo the splice
  newTr.clips.push(cl);
  drag.track = newTr;
  selectedTrackId = newTr.id;
  renderTracks();
  const fresh = els.tracks.querySelector(`.tl-clip[data-clip="${cssEsc(cl.id)}"]`);
  if (!fresh) return;
  fresh.classList.add("dragging");
  drag.clipEl = fresh;
  drag.laneEl = fresh.parentElement;
  drag.laneRect = drag.laneEl.getBoundingClientRect();
  try { fresh.setPointerCapture(drag.pointerId); } catch { /* ignore */ }
  fresh.addEventListener("pointermove", onLanePointerMove);
  fresh.addEventListener("pointerup", onLanePointerUp);
  fresh.addEventListener("pointercancel", onLanePointerUp);
}

function showNewTrackHint(on) {
  if (els && els.newTrackHint) els.newTrackHint.hidden = !on;
}

// -------------------------------------------------------------- drag: auto-scroll
// Start/stop the auto-scroll RAF depending on how close the pointer is to a lane
// viewport edge (spec A3). Only body/trim drags scroll.
function maybeAutoScroll() {
  if (!drag) return;
  if (drag.zone === "fadeIn" || drag.zone === "fadeOut") { stopAutoScroll(); return; }
  const rect = els.lanes.getBoundingClientRect();
  const x = drag.lastClientX;
  const near = x < rect.left + AUTO_EDGE_PX || x > rect.right - AUTO_EDGE_PX;
  if (near && !drag.autoRaf) drag.autoRaf = requestAnimationFrame(autoScrollTick);
  else if (!near && drag.autoRaf) stopAutoScroll();
}

function autoScrollTick() {
  if (!drag) return;
  const rect = els.lanes.getBoundingClientRect();
  const x = drag.lastClientX;
  let dx = 0;
  if (x < rect.left + AUTO_EDGE_PX) dx = -AUTO_STEP_PX;
  else if (x > rect.right - AUTO_EDGE_PX) dx = AUTO_STEP_PX;
  if (dx === 0) { drag.autoRaf = 0; return; }
  els.lanes.scrollLeft = Math.max(0, els.lanes.scrollLeft + dx);
  drag.laneRect = drag.laneEl.getBoundingClientRect();   // scroll moved the lane
  applyDragAt(drag.lastClientX);
  showDragTip(drag.lastClientX, drag.lastClientY, dragTipText(drag.zone, drag.clip, sourceDurOf(drag.clip)));
  drag.autoRaf = requestAnimationFrame(autoScrollTick);
}

function stopAutoScroll() {
  if (drag && drag.autoRaf) { cancelAnimationFrame(drag.autoRaf); drag.autoRaf = 0; }
}

// -------------------------------------------------------------- drag: live trim waveform
function scheduleTrimWave() {
  if (trimWaveRaf) return;
  trimWaveRaf = requestAnimationFrame(() => {
    trimWaveRaf = 0;
    if (!drag || !drag.clipEl) return;
    drawClipWave(drag.clipEl.querySelector(".tl-clip-wave"), drag.clip);
  });
}
function cancelTrimWave() { if (trimWaveRaf) { cancelAnimationFrame(trimWaveRaf); trimWaveRaf = 0; } }

// Roll back and tear down an in-progress drag (Escape while dragging, spec A4).
// Restores the whole timeline from the pre-drag snapshot (covers cross-track
// moves cleanly), keeps the editor open, and pushes NO undo entry.
function cancelDrag() {
  const d = drag;
  if (!d) return;
  if (d.clipEl) {
    d.clipEl.classList.remove("dragging");
    d.clipEl.removeEventListener("pointermove", onLanePointerMove);
    d.clipEl.removeEventListener("pointerup", onLanePointerUp);
    d.clipEl.removeEventListener("pointercancel", onLanePointerUp);
    try { d.clipEl.releasePointerCapture(d.pointerId); } catch { /* ignore */ }
  }
  stopAutoScroll();
  cancelTrimWave();
  drag = null;
  hideDragTip();
  showNewTrackHint(false);
  tl = d.pre;                         // wholesale rollback -- simplest correct restore
  if (!findClip(selectedClipId)) selectedClipId = null;
  if (!track(selectedTrackId)) selectedTrackId = tl.tracks[0] ? tl.tracks[0].id : null;
  renderRuler(); renderTracks(); renderInspector(); updateChip();
}

function onLanePointerUp(e) {
  if (!drag || e.pointerId !== drag.pointerId) return;
  const d = drag;
  const clipEl = d.clipEl;
  const clip = d.clip;
  const moved = d.moved;
  const pre = d.pre;
  const newTrackPending = d.newTrackPending;
  clipEl.classList.remove("dragging");
  clipEl.removeEventListener("pointermove", onLanePointerMove);
  clipEl.removeEventListener("pointerup", onLanePointerUp);
  clipEl.removeEventListener("pointercancel", onLanePointerUp);
  try { clipEl.releasePointerCapture(e.pointerId); } catch { /* ignore */ }
  stopAutoScroll();
  cancelTrimWave();
  showNewTrackHint(false);
  drag = null;
  hideDragTip();

  if (!moved) {
    selectClip(clip.id);
    return;
  }

  // B4/A1: released below all lanes -> move the clip into a fresh track. The
  // single pre-drag snapshot below is the sole undo entry for the whole move.
  if (newTrackPending) {
    const idx = d.track.clips.indexOf(clip);
    if (idx >= 0) d.track.clips.splice(idx, 1);
    const tr = { id: newId(), name: "Track " + (tl.tracks.length + 1), gainDb: 0, mute: false, clips: [] };
    tr.clips.push(clip);
    tl.tracks.push(tr);
    selectedTrackId = tr.id;
  }

  pushSnapshot(pre);              // undo boundary = state before the drag
  selectedClipId = clip.id;
  mutate();
  // A geometry change can grow/shrink the timeline -> rebuild ruler + lanes.
  renderRuler(); renderTracks(); renderInspector(); updateChip();
  rebuildPreview();
}

function selectClip(clipId) {
  selectedClipId = clipId;
  const found = findClip(clipId);
  if (found) selectedTrackId = found.track.id;
  els.tracks.querySelectorAll(".tl-clip").forEach((c) =>
    c.classList.toggle("selected", c.dataset.clip === clipId));
  renderInspector();
}

// Every clip edge (start + end) on every track EXCEPT the dragged clip, for
// butt-join edge snapping. Threshold is applied by the caller (EDGE_PX / zoom).
function otherClipEdges(excludeId) {
  const out = [];
  for (const tr of tl.tracks) {
    for (const cl of tr.clips) {
      if (cl.id === excludeId) continue;
      const s = cl.start || 0;
      out.push(s, s + clipSpan(cl, sourceDurOf(cl)));
    }
  }
  return out;
}

// Floating readout near the cursor during a moved drag, worded by zone.
function dragTipText(zone, cl, sourceDur) {
  if (zone === "body") return "Start " + fmtHMSt(cl.start || 0);
  if (zone === "trimStart" || zone === "trimEnd") return "Length " + fmtHMSt(clipSpan(cl, sourceDur));
  if (zone === "fadeIn") return "Fade " + (cl.fadeIn || 0).toFixed(1) + "s";
  if (zone === "fadeOut") return "Fade " + (cl.fadeOut || 0).toFixed(1) + "s";
  return "";
}

function showDragTip(x, y, text) {
  if (!els || !els.dragTip || !text) return;
  const tip = els.dragTip;
  tip.textContent = text;
  tip.hidden = false;
  tip.style.left = (x + 14) + "px";
  tip.style.top = (y + 14) + "px";
}
function hideDragTip() { if (els && els.dragTip) els.dragTip.hidden = true; }

// Ruler pointer seek (click + drag). Playing -> the engine rebuilds at the new
// time; paused -> just moves the playhead + readout.
function onRulerPointerDown(e) {
  const seekAt = (ev) => {
    const t = Math.max(0, x2t(ev.clientX - els.ruler.getBoundingClientRect().left));
    previewTime = t; preview.seek(t); updatePlayhead(t); renderReadout();
  };
  seekAt(e);
  const move = (ev) => seekAt(ev);
  const up = () => {
    window.removeEventListener("pointermove", move);
    window.removeEventListener("pointerup", up);
  };
  window.addEventListener("pointermove", move);
  window.addEventListener("pointerup", up);
  e.preventDefault();
}

// Ctrl+wheel zoom around the cursor.
function onLaneWheel(e) {
  if (!e.ctrlKey) return;
  e.preventDefault();
  const rect = els.lanes.getBoundingClientRect();
  const cursorContentX = e.clientX - rect.left + els.lanes.scrollLeft;
  const anchorT = cursorContentX / zoom;
  zoom = clampZoom(zoom * (e.deltaY < 0 ? 1.1 : 1 / 1.1));
  renderRuler(); renderTracks();
  els.lanes.scrollLeft = anchorT * zoom - (e.clientX - rect.left);
  updateZoomSlider();
}

// Palette native drag-drop onto a lane (or empty area -> new track).
function onLaneDragOver(e) {
  const types = e.dataTransfer && e.dataTransfer.types;
  if (!types || !Array.from(types).includes("text/plain")) return;
  e.preventDefault();
  e.dataTransfer.dropEffect = "copy";
  els.lanes.classList.add("dragover");
}

function onLaneDrop(e) {
  els.lanes.classList.remove("dragover");
  const raw = e.dataTransfer.getData("text/plain");
  if (!raw || raw.indexOf("tl-src:") !== 0) return;
  e.preventDefault();
  const srcId = raw.slice("tl-src:".length);
  const laneEl = e.target.closest(".tl-lane");
  if (laneEl) {
    const laneRect = laneEl.getBoundingClientRect();
    const t = x2t(e.clientX - laneRect.left);
    addClipFromSource(srcId, laneEl.dataset.lane, t);
  } else {
    // Dropped below all lanes (or onto the empty onboarding area) -> a NEW track
    // at the drop time (spec B4), not the selected track.
    const innerRect = els.inner.getBoundingClientRect();
    const t = Math.max(0, x2t(e.clientX - innerRect.left));
    addClipFromSource(srcId, null, t, { newTrack: true });
  }
}

// -------------------------------------------------------------- undo / redo
function pushSnapshot(pre) {
  undoStack.push(pre);
  if (undoStack.length > UNDO_CAP) undoStack.shift();
  redoStack = [];
  updateHistButtons();
}

function updateHistButtons() {
  if (!els) return;
  els.undo.disabled = !undoStack.length;
  els.redo.disabled = !redoStack.length;
}

function undo() {
  if (!undoStack.length) return;
  redoStack.push(deepCloneTimeline(tl));
  tl = undoStack.pop();
  afterHistory();
}

function redo() {
  if (!redoStack.length) return;
  undoStack.push(deepCloneTimeline(tl));
  tl = redoStack.pop();
  afterHistory();
}

function afterHistory() {
  if (!findClip(selectedClipId)) selectedClipId = null;
  if (!track(selectedTrackId)) selectedTrackId = tl.tracks[0] ? tl.tracks[0].id : null;
  mutate();                                   // counts as a mutation -> debounced updateTimeline
  renderRuler(); renderTracks(); renderInspector(); updateChip(); updateHistButtons();
  rebuildPreview();                           // restart preview at current playhead
}

// One undo entry per field-edit session: stash pre on focus, push on change.
function historyField(el) {
  let pre = null;
  el.addEventListener("focus", () => { pre = deepCloneTimeline(tl); });
  el.addEventListener("change", () => { if (pre) { pushSnapshot(pre); pre = null; } });
}

// -------------------------------------------------------------- send / actions
function mutate() {
  dirty = true;
  scheduleSend();
  renderReadout();   // edits can change the content length (transport total)
}

function scheduleSend() {
  clearTimeout(sendTimer);
  sendTimer = setTimeout(flushSend, DEBOUNCE_MS);
}

function flushSend() {
  clearTimeout(sendTimer);
  sendTimer = null;
  if (!dirty || !editingId) return;
  dirty = false;
  send("updateTimeline", { itemId: editingId, timeline: tl });
}

// -------------------------------------------------------------- keyboard / close
function onKeydown(e) {
  if (!overlay) return;

  // An arrow-nudge undo session ends on any non-arrow key (spec B5).
  if (arrowSession && e.key !== "ArrowLeft" && e.key !== "ArrowRight") endArrowSession();

  // Escape while dragging cancels the drag (spec A4) -- must beat the field-blur
  // / close-editor Escape handling below.
  if (e.key === "Escape" && drag) {
    e.preventDefault(); e.stopPropagation();
    cancelDrag();
    return;
  }

  if (e.code === "Space") {
    if (isTypingTarget(document.activeElement)) return;   // typing in a field -> normal space
    e.preventDefault(); e.stopPropagation();              // also stops native button re-click
    togglePreview();
    return;
  }
  if (e.key === "z" && (e.ctrlKey || e.metaKey) && !e.shiftKey) {
    if (!isTypingTarget(document.activeElement)) { e.preventDefault(); undo(); }
    return;
  }
  if ((e.key === "y" && (e.ctrlKey || e.metaKey)) ||
      (e.key === "z" && (e.ctrlKey || e.metaKey) && e.shiftKey)) {
    if (!isTypingTarget(document.activeElement)) { e.preventDefault(); redo(); }
    return;
  }

  // Delete / nudge the selected clip (guarded against typing in a field).
  if (!isTypingTarget(document.activeElement) && selectedClipId) {
    if (e.key === "Delete" || e.key === "Backspace") {
      e.preventDefault(); deleteClip(selectedClipId); return;
    }
    if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
      e.preventDefault(); nudgeSelectedClip(e.key === "ArrowLeft" ? -1 : 1, e.shiftKey); return;
    }
  }

  if (e.key !== "Escape") return;
  const ae = document.activeElement;
  if (ae && overlay.contains(ae) && (ae.tagName === "INPUT" || ae.tagName === "SELECT")) {
    ae.blur();               // first Escape leaves the field, not the editor
    e.stopPropagation();
    return;
  }
  e.stopPropagation();
  e.preventDefault();
  close();
}

function close() {
  if (!overlay) return;
  flushSend();
  if (preview) { preview.destroy(); preview = null; }
  destroyed = true;
  stopAutoScroll();
  cancelTrimWave();
  endArrowSession();
  if (unsub) { unsub(); unsub = null; }
  document.removeEventListener("keydown", onKeydown, true);
  overlay.remove();
  overlay = null;
  els = null;
  tl = null;
  editingId = null;
  selectedClipId = null;
  drag = null;
  undoStack = [];
  redoStack = [];
}
