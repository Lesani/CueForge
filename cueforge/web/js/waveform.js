// CueForge waveform widget (canvas-based, no external library).
//
// Fetches + decodes the item's stored audio (via audioCache), draws min/max
// peaks from channel 0, and renders two draggable vertical trim handles
// (in/out). Adds a zoomable/scrollable view window with a time ruler, a
// toolbar (Fit / zoom / zoom-to-selection / bars), a pan scrollbar, and
// automatic 4/4 beat (Takt) bar lines. Dragging a handle shades the selected
// region live; releasing calls back with the new trim seconds. No dependency
// on wavesurfer.js -- this build uses a small in-house canvas implementation
// to keep the "no build step" constraint trivial.

import { getAudioBuffer } from "./audioCache.js";
import {
  PAD, innerWidth, timeToX as viewTimeToX, xToTime as viewXToTime,
  clampView, zoomAbout, fitView, selectionView, chooseTickInterval, barTimes,
} from "./waveformView.js";
import { detectTempo } from "./beatDetect.js";
import { envAt } from "./fadeEnvelope.js";

// Pointer distance (px) within which a click grabs a handle -- generous so the
// thin handles are easy to grab, especially on touch.
const HANDLE_HIT_PX = 14;
// Time ruler band height (px) reserved at the top of the canvas.
const RULER_H = 18;
// Fade dot radius (px) and the top band (below the ruler) within which a click
// grabs a fade dot instead of a trim handle.
const FADE_DOT_R = 5;
const FADE_BAND_H = 16;
// Max zoom: smallest visible window (seconds).
const MIN_VIEW_DUR = 0.05;
// Autocorrelation confidence at/above which bars auto-show (and a BPM is shown).
const CONF_THRESHOLD = 0.4;

// Detected tempo cached by audioHash so re-selecting an item is instant
// (mirrors the peaks-per-load pattern, but survives widget rebuilds).
const tempoCache = new Map(); // audioHash -> { bpm, firstBeatSec, confidence }

/**
 * createWaveform(container, { audioHash, duration, trimIn, trimOut, onTrimChange, getPlayheadSeconds })
 * -> { destroy(), setTrim(trimIn, trimOut), setPlaying(bool) }
 */
export function createWaveform(container, opts) {
  const audioHash = opts.audioHash;
  const duration = Math.max(0, Number(opts.duration) || 0);
  const onTrimChange = typeof opts.onTrimChange === "function" ? opts.onTrimChange : () => {};
  const onFadeChange = typeof opts.onFadeChange === "function" ? opts.onFadeChange : () => {};
  // Returns the current playhead position in absolute waveform seconds, or null
  // to hide the marker. Only polled while setPlaying(true) drives the rAF loop.
  const getPlayheadSeconds = typeof opts.getPlayheadSeconds === "function" ? opts.getPlayheadSeconds : null;

  let trimIn = clamp(Number(opts.trimIn) || 0, 0, duration);
  let trimOut = clamp(
    opts.trimOut && Number(opts.trimOut) > 0 ? Number(opts.trimOut) : duration,
    0, duration
  );
  // Fade lengths (seconds), measured from the trim edges inward, plus curve
  // shape. Drive the envelope-scaled static peaks + the draggable fade dots.
  let fadeIn = Math.max(0, Number(opts.fadeIn) || 0);
  let fadeOut = Math.max(0, Number(opts.fadeOut) || 0);
  let fadeShape = opts.fadeShape === "equalPower" ? "equalPower" : "linear";

  let destroyed = false;
  let buffer = null;
  let peaks = null;     // { mins:Float32Array, maxs:Float32Array } sized to last-drawn inner width
  let dragging = null;  // "in" | "out" | "fadeIn" | "fadeOut" | null
  let playing = false;  // whether the playhead marker loop is running
  let rafId = null;     // requestAnimationFrame handle for the marker loop

  // View window (zoom/pan) + interaction state.
  let view = fitView(duration, MIN_VIEW_DUR);   // { start, dur } in seconds
  let marquee = null;                            // { t0, t1 } while drag-selecting a zoom region
  let zoomSelectMode = false;                    // "Zoom to selection" toggle
  let scrollDrag = null;                         // scrollbar thumb drag anchor

  // Touch gesture state (pinch zoom / one-finger pan). Mouse input never
  // populates `pointers`, so all of these paths stay dormant for the mouse and
  // its behaviour is unchanged.
  const pointers = new Map();   // active touch/pen pointerId -> { x, y } client coords
  let pinch = null;             // { startDistX, startView, anchorT } during a two-finger pinch
  let onePan = null;            // { id, lastX } during a one-finger pan
  let dragStartTrim = null;     // pre-drag trim snapshot, to revert a trim drag cancelled by a pinch

  // Tempo / bars.
  let tempo = null;             // { bpm, firstBeatSec, confidence } | null
  let barsVisible = false;      // whether Takt lines are drawn
  let barsUserToggled = false;  // once true, the user's Bars choice overrides the confidence default

  // ---- DOM: toolbar, framed canvas, pan scrollbar ----
  container.classList.add("waveform");
  container.innerHTML = "";

  const toolbar = document.createElement("div");
  toolbar.className = "wf-toolbar";
  toolbar.innerHTML = `
    <button type="button" class="wf-btn" data-wf="fit" title="Zoom to fit">Fit</button>
    <button type="button" class="wf-btn" data-wf="out" title="Zoom out">&minus;</button>
    <button type="button" class="wf-btn" data-wf="in" title="Zoom in">+</button>
    <button type="button" class="wf-btn" data-wf="sel" title="Zoom to selection (drag a box)">&#10530; Zoom</button>
    <button type="button" class="wf-btn" data-wf="bars" title="Show/hide bar lines">Bars</button>
    <span class="wf-tempo" data-wf-tempo>Tempo: &mdash;</span>`;
  container.appendChild(toolbar);

  const wrap = document.createElement("div");
  wrap.className = "wf-canvas-wrap";
  const canvas = document.createElement("canvas");
  canvas.className = "waveform-canvas";
  // Own all touch gestures on the strip -- the canvas lives in its own frame and
  // does not need page scroll/zoom, so this lets pinch/pan pointer events land
  // here instead of the browser hijacking them.
  canvas.style.touchAction = "none";
  wrap.appendChild(canvas);
  const status = document.createElement("div");
  status.className = "waveform-status";
  status.textContent = "Loading waveform...";
  wrap.appendChild(status);
  container.appendChild(wrap);

  const scrollbar = document.createElement("div");
  scrollbar.className = "wf-scroll";
  scrollbar.innerHTML = `<div class="wf-scroll-thumb"></div>`;
  container.appendChild(scrollbar);

  const tempoEl = toolbar.querySelector("[data-wf-tempo]");
  const thumb = scrollbar.querySelector(".wf-scroll-thumb");

  // Cache the 2D context and CSS colors once. Re-reading them per frame
  // (getContext / getComputedStyle) forces style recalc and is a main source of
  // the choppy playhead sweep.
  const ctx = canvas.getContext("2d");
  const accentColor = cssVar("--accent", "#4C8DD6");
  const violetColor = cssVar("--bg-cue", "#9B7FE0");
  const fadeColor = cssVar("--fade-cue", "#E0A84C");

  // Static layer: an offscreen canvas (plain <canvas>, not OffscreenCanvas, for
  // Safari) holding background + ruler + peaks -- everything that changes only
  // on view/zoom/pan/resize/buffer changes. draw() blits this once, then paints
  // the cheap dynamic overlay (shade, bars, marquee, playhead, handles) on top.
  // Rebuilt lazily whenever invalidated.
  const staticCanvas = document.createElement("canvas");
  const staticCtx = staticCanvas.getContext("2d");
  let staticValid = false;

  // Canvas CSS size + dpr, updated ONLY by measureCanvas() (initial mount +
  // ResizeObserver) so draw()/the rAF tick never call getBoundingClientRect or
  // write canvas.style -- both force layout and stutter the animation.
  let cssW = 0, cssH = 0, dpr = 1;

  function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

  // View-relative coordinate helpers (width = current canvas CSS px).
  function tX(t, w) { return viewTimeToX(t, view, w); }
  function xT(x, w) { return viewXToTime(x, view, w); }
  function pxPerSecLocal(w) { return innerWidth(w) / (view.dur > 0 ? view.dur : 1); }
  function setView(v) {
    view = clampView(v, duration, MIN_VIEW_DUR);
    peaks = null;
    invalidateStatic();
    draw();
    syncScrollbar();
  }

  // Peaks for the VISIBLE window only, at screen resolution (sharper on zoom).
  function computePeaks(width) {
    if (!buffer || width <= 0) return null;
    const ch0 = buffer.getChannelData(0);
    const total = ch0.length;
    const sr = buffer.sampleRate;
    const startSample = Math.max(0, Math.floor(view.start * sr));
    const endSample = Math.min(total, Math.ceil((view.start + view.dur) * sr));
    const span = Math.max(1, endSample - startSample);
    const mins = new Float32Array(width);
    const maxs = new Float32Array(width);
    const perPixel = span / width;
    for (let x = 0; x < width; x++) {
      const s = startSample + Math.floor(x * perPixel);
      const e = Math.min(endSample, Math.max(s + 1, startSample + Math.floor((x + 1) * perPixel)));
      let mn = 0, mx = 0;
      for (let i = s; i < e; i++) { const v = ch0[i]; if (v < mn) mn = v; if (v > mx) mx = v; }
      mins[x] = mn; maxs[x] = mx;
    }
    return { mins, maxs };
  }

  function cssVar(name, fallback) {
    const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallback;
  }

  // Mark the static layer stale so the next draw() rebuilds it. Cheap; call it
  // whenever background/ruler/peaks change (view, resize, buffer load).
  function invalidateStatic() { staticValid = false; }

  // Owns ALL layout-forcing reads/writes: measure the canvas, and when the CSS
  // size or dpr changed, resize both the visible and static canvases (at dpr
  // resolution for crispness) and invalidate. Called from mount + the
  // ResizeObserver only -- never from draw() or the rAF tick. Returns true if
  // the size changed.
  function measureCanvas() {
    const rect = canvas.getBoundingClientRect();
    const w = Math.max(1, Math.floor(rect.width));
    const h = Math.max(1, Math.floor(rect.height || 140));
    const d = window.devicePixelRatio || 1;
    if (w === cssW && h === cssH && d === dpr) return false;
    cssW = w; cssH = h; dpr = d;
    canvas.width = w * d;
    canvas.height = h * d;
    canvas.style.width = w + "px";
    canvas.style.height = h + "px";
    staticCanvas.width = w * d;
    staticCanvas.height = h * d;
    peaks = null;         // inner width changed -> recompute at new resolution
    invalidateStatic();
    return true;
  }

  // Rebuild the static layer (background + ruler + peaks) into staticCanvas.
  function rebuildStatic() {
    const g = staticCtx;
    const w = cssW, h = cssH;
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, w, h);

    g.fillStyle = "rgba(255,255,255,0.03)";
    g.fillRect(0, RULER_H, w, h - RULER_H);

    // Time ruler band (top).
    drawRuler(g, w);

    // Peaks (below the ruler).
    const iw = innerWidth(w);
    if (!peaks || peaks.mins.length !== iw) peaks = computePeaks(iw);
    if (peaks) {
      const mid = RULER_H + (h - RULER_H) / 2;
      const amp = (h - RULER_H) / 2 * 0.92;
      const span = Math.max(0, trimOut - trimIn);
      // env for inner pixel x: 1 outside the trim window (that region is dimmed
      // by the shade overlay, not the fade), else the fade envelope multiplier.
      const envAtX = (x) => {
        const t = xT(PAD + x, w);
        if (t < trimIn || t > trimOut) return 1;
        return envAt(t - trimIn, fadeIn, fadeOut, span, fadeShape);
      };
      g.strokeStyle = accentColor;
      g.lineWidth = 1;
      g.beginPath();
      for (let x = 0; x < iw; x++) {
        const env = envAtX(x);
        const mn = peaks.mins[x], mx = peaks.maxs[x];
        const y1 = mid - mx * amp * env;
        const y2 = mid - mn * amp * env;
        const sx = PAD + x;
        g.moveTo(sx + 0.5, Math.min(y1, y2));
        g.lineTo(sx + 0.5, Math.max(y1, y2) + 0.5);
      }
      g.stroke();
      // Thin fade contour tracing the upper envelope, ONLY across [trimIn,trimOut]
      // (amendment 3): start a fresh segment whenever we (re)enter the window.
      if (span > 0 && (fadeIn > 0 || fadeOut > 0)) {
        g.strokeStyle = "rgba(255,255,255,0.5)";
        g.beginPath();
        let inSeg = false;
        for (let x = 0; x < iw; x++) {
          const t = xT(PAD + x, w);
          if (t < trimIn || t > trimOut) { inSeg = false; continue; }
          const y = mid - amp * envAt(t - trimIn, fadeIn, fadeOut, span, fadeShape);
          const sx = PAD + x + 0.5;
          if (!inSeg) { g.moveTo(sx, y); inSeg = true; } else g.lineTo(sx, y);
        }
        g.stroke();
      }
    }
    staticValid = true;
  }

  // Per-frame paint: blit the cached static layer, then the cheap dynamic
  // overlay. Does NO layout reads and no style writes, so it stays smooth in
  // the rAF playhead loop. Z-order matches the old single-pass draw exactly:
  // background/ruler/peaks (static), shade, bars, marquee, playhead, handles.
  function draw() {
    if (destroyed || cssW === 0) return;
    if (!staticValid) rebuildStatic();
    const g = ctx;
    const w = cssW, h = cssH;
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, w, h);
    // staticCanvas is at physical (dpr) resolution; drawing it at CSS size under
    // the dpr transform maps its pixels 1:1 onto the visible canvas.
    g.drawImage(staticCanvas, 0, 0, w, h);

    // Shade the trimmed-out (excluded) regions (only below the ruler).
    const xIn = clamp(tX(trimIn, w), 0, w);
    const xOut = clamp(tX(trimOut, w), 0, w);
    g.fillStyle = "rgba(0,0,0,0.6)";
    if (xIn > 0) g.fillRect(0, RULER_H, xIn, h - RULER_H);
    if (xOut < w) g.fillRect(xOut, RULER_H, w - xOut, h - RULER_H);

    // Bar (Takt) lines, drawn over peaks/shade but under the trim grips. Kept
    // dynamic so this pass preserves the old z-order (shade under bars) without
    // rebuilding peaks; only the visible bars are stroked, so it stays cheap.
    drawBars(g, w, h);

    // Marquee zoom overlay.
    if (marquee) {
      const mx0 = clamp(tX(marquee.t0, w), 0, w);
      const mx1 = clamp(tX(marquee.t1, w), 0, w);
      const a = Math.min(mx0, mx1), b = Math.max(mx0, mx1);
      g.fillStyle = "rgba(255,255,255,0.12)";
      g.fillRect(a, RULER_H, b - a, h - RULER_H);
      g.strokeStyle = "rgba(255,255,255,0.5)";
      g.lineWidth = 1;
      g.strokeRect(a + 0.5, RULER_H + 0.5, b - a - 1, h - RULER_H - 1);
    }

    // Playhead marker (over peaks/shade/bars but under the trim grips).
    if (playing && getPlayheadSeconds) {
      const t = getPlayheadSeconds();
      if (t != null && isFinite(t)) {
        const px = tX(clamp(t, 0, duration), w);
        if (px >= PAD - 1 && px <= w - PAD + 1) drawPlayhead(g, px, h);
      }
    }

    drawHandle(g, tX(trimIn, w), h, violetColor, dragging === "in");
    drawHandle(g, tX(trimOut, w), h, violetColor, dragging === "out");

    // Fade dots at the fade line (trimIn+fadeIn / trimOut-fadeOut), in the top
    // band. Drawn over the handles so a zero-length fade dot sits on the corner.
    drawFadeDot(g, tX(trimIn + fadeIn, w), dragging === "fadeIn");
    drawFadeDot(g, tX(trimOut - fadeOut, w), dragging === "fadeOut");
  }

  // A small round grab dot centred in the top fade band at pixel x.
  function drawFadeDot(g, x, active) {
    if (x < PAD - FADE_DOT_R || x > cssW - PAD + FADE_DOT_R) return; // off-screen
    const y = RULER_H + FADE_BAND_H / 2;
    g.beginPath();
    g.arc(x, y, active ? FADE_DOT_R + 1 : FADE_DOT_R, 0, Math.PI * 2);
    g.fillStyle = fadeColor;
    g.fill();
    g.lineWidth = 1;
    g.strokeStyle = "rgba(0,0,0,0.55)";
    g.stroke();
  }

  // Time ruler: nice-interval major ticks + labels, quarter minor ticks.
  function drawRuler(g, w) {
    g.fillStyle = "rgba(255,255,255,0.05)";
    g.fillRect(0, 0, w, RULER_H);
    const iv = chooseTickInterval(view, w, 80);
    const first = Math.ceil(view.start / iv) * iv;
    const end = view.start + view.dur;
    g.strokeStyle = "rgba(255,255,255,0.18)";
    g.lineWidth = 1;
    g.font = "10px system-ui, sans-serif";
    g.textBaseline = "middle";
    for (let t = first; t <= end + 1e-9; t += iv) {
      const x = tX(t, w);
      g.beginPath(); g.moveTo(x + 0.5, 0); g.lineTo(x + 0.5, RULER_H); g.stroke();
      for (let m = 1; m < 4; m++) {
        const xm = tX(t + (iv * m) / 4, w);
        g.beginPath(); g.moveTo(xm + 0.5, RULER_H - 4); g.lineTo(xm + 0.5, RULER_H); g.stroke();
      }
      g.fillStyle = "rgba(255,255,255,0.6)";
      g.fillText(fmtTime(t, iv), x + 3, RULER_H / 2);
    }
  }

  function fmtTime(t, iv) {
    if (iv < 1) return t.toFixed(2) + "s";
    const m = Math.floor(t / 60), s = Math.floor(t % 60);
    return m + ":" + String(s).padStart(2, "0");
  }

  // 4/4 bar (Takt) lines + faint intra-bar beat subdivisions.
  function drawBars(g, w, h) {
    if (!barsVisible || !tempo || !(tempo.bpm > 0)) return;
    const bars = barTimes(tempo.bpm, tempo.firstBeatSec, duration, 4);
    const beatDur = 60 / tempo.bpm;
    g.textBaseline = "top";
    g.font = "10px system-ui, sans-serif";
    for (const bar of bars) {
      const x = tX(bar.time, w);
      if (x < PAD - 1 || x > w - PAD + 1) continue;
      // faint beat subdivisions (beats 2,3,4 within this bar)
      g.strokeStyle = "rgba(255,255,255,0.10)";
      g.lineWidth = 1;
      for (let b = 1; b < 4; b++) {
        const bx = tX(bar.time + b * beatDur, w);
        if (bx < PAD || bx > w - PAD) continue;
        g.beginPath(); g.moveTo(bx + 0.5, RULER_H); g.lineTo(bx + 0.5, h); g.stroke();
      }
      // bar (Takt) line
      g.strokeStyle = "rgba(155,127,224,0.55)"; // violet, matches --bg-cue
      g.lineWidth = 1;
      g.beginPath(); g.moveTo(x + 0.5, RULER_H); g.lineTo(x + 0.5, h); g.stroke();
      g.fillStyle = "rgba(200,190,240,0.9)";
      g.fillText(String(bar.number), x + 3, RULER_H + 2);
    }
  }

  // A bright vertical marker with a small triangle nub at the ruler edge.
  function drawPlayhead(g, x, h) {
    g.strokeStyle = "rgba(255,255,255,0.85)";
    g.lineWidth = 2;
    g.beginPath();
    g.moveTo(x, RULER_H);
    g.lineTo(x, h);
    g.stroke();
    g.fillStyle = "rgba(255,255,255,0.85)";
    g.beginPath();
    g.moveTo(x - 4, RULER_H);
    g.lineTo(x + 4, RULER_H);
    g.lineTo(x, RULER_H + 6);
    g.closePath();
    g.fill();
  }

  // rAF loop: redraw every frame while playing so the marker tracks the clock
  // smoothly. Self-stops when setPlaying(false) or on destroy.
  function tick() {
    if (destroyed || !playing) { rafId = null; return; }
    draw();
    rafId = requestAnimationFrame(tick);
  }

  // A grabbable trim handle: full-height bar + a rounded grip pill so the
  // target reads clearly and is easy to hit.
  function drawHandle(g, x, h, color, active) {
    if (x < -8 || x > cssW + 8) return; // off-screen when zoomed
    const barW = active ? 4 : 3;
    g.fillStyle = color;
    g.fillRect(x - barW / 2, RULER_H, barW, h - RULER_H);
    // Grip pill centred vertically in the waveform body.
    const gw = 10, gh = 26;
    const gx = x - gw / 2;
    const gy = RULER_H + (h - RULER_H) / 2 - gh / 2;
    g.fillStyle = "#0c0e11";
    roundRect(g, gx - 1, gy - 1, gw + 2, gh + 2, 5);
    g.fill();
    g.fillStyle = color;
    roundRect(g, gx, gy, gw, gh, 4);
    g.fill();
    // Two grip lines.
    g.strokeStyle = "rgba(0,0,0,0.55)";
    g.lineWidth = 1;
    g.beginPath();
    g.moveTo(x - 2, gy + 7); g.lineTo(x - 2, gy + gh - 7);
    g.moveTo(x + 2, gy + 7); g.lineTo(x + 2, gy + gh - 7);
    g.stroke();
  }

  function roundRect(g, x, y, w, h, r) {
    const rr = Math.min(r, w / 2, h / 2);
    g.beginPath();
    g.moveTo(x + rr, y);
    g.arcTo(x + w, y, x + w, y + h, rr);
    g.arcTo(x + w, y + h, x, y + h, rr);
    g.arcTo(x, y + h, x, y, rr);
    g.arcTo(x, y, x + w, y, rr);
    g.closePath();
  }

  function handleAt(x, width) {
    const xIn = tX(trimIn, width);
    const xOut = tX(trimOut, width);
    const dIn = Math.abs(x - xIn);
    const dOut = Math.abs(x - xOut);
    if (dIn <= HANDLE_HIT_PX && dIn <= dOut) return "in";
    if (dOut <= HANDLE_HIT_PX) return "out";
    return null;
  }

  // Fade-dot hit-test: only inside the top fade band, nearest dot within
  // HANDLE_HIT_PX. Wins over handleAt (checked first in onPointerDown) so the
  // top corners grab the fade even when they coincide with a trim handle.
  function fadeHandleAt(x, y, width) {
    if (y < RULER_H || y > RULER_H + FADE_BAND_H) return null;
    const xFi = tX(trimIn + fadeIn, width);
    const xFo = tX(trimOut - fadeOut, width);
    const dFi = Math.abs(x - xFi);
    const dFo = Math.abs(x - xFo);
    if (dFi <= HANDLE_HIT_PX && dFi <= dFo) return "fadeIn";
    if (dFo <= HANDLE_HIT_PX) return "fadeOut";
    return null;
  }

  // ---- pointer: trim-drag vs marquee-zoom vs touch pinch/pan ----
  // Touch and pen use the multi-touch gesture paths (pinch zoom, one-finger
  // pan); mouse keeps the exact behaviour it has always had.
  function isTouch(e) { return e.pointerType === "touch" || e.pointerType === "pen"; }

  // Revert an in-progress trim drag to its pre-drag values WITHOUT notifying,
  // used when a second finger turns a trim drag into a pinch.
  function cancelTrimDrag() {
    if (!dragging) return;
    if (dragStartTrim) {
      trimIn = dragStartTrim.trimIn;
      trimOut = dragStartTrim.trimOut;
      if (dragStartTrim.fadeIn != null) fadeIn = dragStartTrim.fadeIn;
      if (dragStartTrim.fadeOut != null) fadeOut = dragStartTrim.fadeOut;
    }
    dragging = null;
    dragStartTrim = null;
    invalidateStatic();
    draw();
  }
  function cancelMarquee() {
    if (!marquee) return;
    marquee = null;
    draw();
  }

  // Snapshot the two live touch points as the pinch baseline: horizontal span
  // (vertical is meaningless on a 140px strip) plus the time under the gesture
  // centre, which is held fixed as the zoom/pan anchor.
  function beginPinch(rect) {
    const pts = [...pointers.values()].slice(0, 2);
    const x0 = pts[0].x - rect.left, x1 = pts[1].x - rect.left;
    const centerX = (x0 + x1) / 2;
    pinch = {
      startDistX: Math.max(1, Math.abs(x1 - x0)),
      startView: { start: view.start, dur: view.dur },
      anchorT: viewXToTime(centerX, view, rect.width),
    };
  }
  // Zoom by the change in horizontal finger spread and pan by the centre's
  // movement, keeping anchorT pinned under the current gesture centre.
  function updatePinch(rect) {
    const pts = [...pointers.values()].slice(0, 2);
    const x0 = pts[0].x - rect.left, x1 = pts[1].x - rect.left;
    const distX = Math.max(1, Math.abs(x1 - x0));
    const centerX = (x0 + x1) / 2;
    const wanted = clampView(
      { start: 0, dur: pinch.startView.dur * (pinch.startDistX / distX) },
      duration, MIN_VIEW_DUR
    );
    const pps = innerWidth(rect.width) / (wanted.dur > 0 ? wanted.dur : 1);
    setView({ start: pinch.anchorT - (centerX - PAD) / pps, dur: wanted.dur });
  }

  function onPointerDown(e) {
    const rect = canvas.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const y = e.clientY - rect.top;
    if (isTouch(e)) {
      pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
      // Second finger down -> begin a pinch, cancelling any single-pointer
      // gesture already in progress (trim drag reverts silently; marquee drops).
      if (pointers.size === 2) {
        cancelTrimDrag();
        cancelMarquee();
        onePan = null;
        beginPinch(rect);
        try { canvas.setPointerCapture(e.pointerId); } catch { /* ignore */ }
        e.preventDefault();
        return;
      }
      if (pointers.size > 2) { e.preventDefault(); return; } // ignore extra fingers
    }
    if (zoomSelectMode) {
      const t = xT(x, rect.width);
      marquee = { t0: t, t1: t };
      try { canvas.setPointerCapture(e.pointerId); } catch { /* ignore */ }
      e.preventDefault();
      return;
    }
    // Fade dots (top band) win over trim handles at a shared x.
    const fh = fadeHandleAt(x, y, rect.width);
    if (fh) {
      dragging = fh;
      dragStartTrim = { trimIn, trimOut, fadeIn, fadeOut };
      try { canvas.setPointerCapture(e.pointerId); } catch { /* ignore */ }
      e.preventDefault();
      return;
    }
    const hnd = handleAt(x, rect.width);
    if (hnd) {
      dragging = hnd;
      dragStartTrim = { trimIn, trimOut, fadeIn, fadeOut };
      try { canvas.setPointerCapture(e.pointerId); } catch { /* ignore */ }
      e.preventDefault();
      return;
    }
    // Empty waveform: mouse does nothing (as today); a single touch begins a
    // one-finger pan (a no-op until zoomed, handled in onPointerMove).
    if (isTouch(e)) {
      onePan = { id: e.pointerId, lastX: x };
      try { canvas.setPointerCapture(e.pointerId); } catch { /* ignore */ }
      e.preventDefault();
    }
  }

  function onPointerMove(e) {
    const rect = canvas.getBoundingClientRect();
    const x = e.clientX - rect.left;
    if (isTouch(e) && pointers.has(e.pointerId)) {
      pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    }
    // Two-finger pinch/pan takes priority over everything else.
    if (pinch && pointers.size >= 2) { updatePinch(rect); e.preventDefault(); return; }
    // One-finger pan (only shifts the view when zoomed in).
    if (onePan && e.pointerId === onePan.id) {
      const dx = x - onePan.lastX;
      onePan.lastX = x;
      if (dx && view.dur < duration - 1e-9) {
        setView({ start: view.start - dx / pxPerSecLocal(rect.width), dur: view.dur });
      }
      e.preventDefault();
      return;
    }
    if (marquee) { marquee.t1 = xT(x, rect.width); draw(); return; }
    if (!dragging) return;
    const t = xT(x, rect.width);
    if (dragging === "fadeIn" || dragging === "fadeOut") {
      const span = Math.max(0, trimOut - trimIn);
      if (dragging === "fadeIn") fadeIn = Math.max(0, Math.min(span - fadeOut, t - trimIn));
      else fadeOut = Math.max(0, Math.min(span - fadeIn, trimOut - t));
      invalidateStatic();   // envelope-scaled peaks + contour must re-stroke
      draw();
      return;
    }
    if (dragging === "in") {
      trimIn = Math.min(t, trimOut - 0.02 > 0 ? trimOut - 0.02 : 0);
      if (trimIn < 0) trimIn = 0;
    } else {
      trimOut = Math.max(t, trimIn + 0.02);
      if (trimOut > duration) trimOut = duration;
    }
    invalidateStatic();     // trim changes the fade window -> re-scale peaks
    draw();
  }

  function onPointerUp(e) {
    if (isTouch(e)) pointers.delete(e.pointerId);
    // A finger lifting out of a pinch: if one finger remains it continues as a
    // one-finger pan (re-anchored to the survivor); otherwise the pinch ends.
    if (pinch) {
      try { canvas.releasePointerCapture(e.pointerId); } catch { /* ignore */ }
      if (pointers.size >= 2) { beginPinch(canvas.getBoundingClientRect()); return; }
      pinch = null;
      if (pointers.size === 1) {
        const [id, p] = [...pointers.entries()][0];
        onePan = { id, lastX: p.x - canvas.getBoundingClientRect().left };
      }
      return;
    }
    if (onePan && e.pointerId === onePan.id) {
      onePan = null;
      try { canvas.releasePointerCapture(e.pointerId); } catch { /* ignore */ }
      return;
    }
    if (marquee) {
      const m = marquee;
      marquee = null;
      setZoomSelect(false);
      try { canvas.releasePointerCapture(e.pointerId); } catch { /* ignore */ }
      if (Math.abs(m.t1 - m.t0) > 0.01) setView(selectionView(m.t0, m.t1, duration, MIN_VIEW_DUR));
      else draw();
      return;
    }
    if (!dragging) return;
    const was = dragging;
    dragging = null;
    dragStartTrim = null;
    try { canvas.releasePointerCapture(e.pointerId); } catch { /* ignore */ }
    if (was === "fadeIn" || was === "fadeOut") onFadeChange({ fadeIn, fadeOut });
    else onTrimChange({ trimIn, trimOut });
  }

  // ---- wheel: Ctrl = zoom at cursor, else pan ----
  function onWheel(e) {
    const rect = canvas.getBoundingClientRect();
    if (e.ctrlKey) {
      e.preventDefault();
      const anchorT = xT(e.clientX - rect.left, rect.width);
      setView(zoomAbout(view, e.deltaY > 0 ? 1.15 : 1 / 1.15, anchorT, duration, MIN_VIEW_DUR));
    } else {
      const dt = ((e.deltaX || e.deltaY) / pxPerSecLocal(rect.width)) * 0.5;
      if (dt) { e.preventDefault(); setView({ start: view.start + dt, dur: view.dur }); }
    }
  }

  // ---- toolbar ----
  function setZoomSelect(on) {
    zoomSelectMode = !!on;
    toolbar.querySelector('[data-wf="sel"]').classList.toggle("active", zoomSelectMode);
    canvas.style.cursor = zoomSelectMode ? "crosshair" : "ew-resize";
  }

  function onToolbarClick(e) {
    const btn = e.target.closest("[data-wf]");
    if (!btn) return;
    const centerT = view.start + view.dur / 2;
    switch (btn.dataset.wf) {
      case "fit": setView(fitView(duration, MIN_VIEW_DUR)); break;
      case "in": setView(zoomAbout(view, 1 / 1.6, centerT, duration, MIN_VIEW_DUR)); break;
      case "out": setView(zoomAbout(view, 1.6, centerT, duration, MIN_VIEW_DUR)); break;
      case "sel": setZoomSelect(!zoomSelectMode); break;
      case "bars":
        barsUserToggled = true;
        barsVisible = !barsVisible;
        toolbar.querySelector('[data-wf="bars"]').classList.toggle("active", barsVisible);
        draw();
        break;
    }
  }

  function onKeyDown(e) {
    if (e.key === "Escape" && zoomSelectMode) {
      setZoomSelect(false);
      marquee = null;
      draw();
    }
  }

  // ---- pan scrollbar ----
  function syncScrollbar() {
    const zoomed = duration > 0 && view.dur < duration - 1e-9;
    scrollbar.style.visibility = zoomed ? "visible" : "hidden";
    if (!zoomed) return;
    thumb.style.left = (100 * view.start / duration) + "%";
    thumb.style.width = (100 * view.dur / duration) + "%";
  }

  function onThumbDown(e) {
    scrollDrag = { x: e.clientX, start: view.start };
    try { thumb.setPointerCapture(e.pointerId); } catch { /* ignore */ }
    e.preventDefault();
  }
  function onThumbMove(e) {
    if (!scrollDrag) return;
    const w = scrollbar.getBoundingClientRect().width || 1;
    const dt = ((e.clientX - scrollDrag.x) / w) * duration;
    setView({ start: scrollDrag.start + dt, dur: view.dur });
  }
  function onThumbUp(e) {
    scrollDrag = null;
    try { thumb.releasePointerCapture(e.pointerId); } catch { /* ignore */ }
  }

  // ---- tempo detection ----
  function applyTempo(result) {
    tempo = result || null;
    if (!barsUserToggled) barsVisible = !!(tempo && tempo.confidence >= CONF_THRESHOLD);
    toolbar.querySelector('[data-wf="bars"]').classList.toggle("active", barsVisible);
    if (tempo && tempo.confidence >= CONF_THRESHOLD) {
      tempoEl.textContent = "~" + Math.round(tempo.bpm) + " BPM · 4/4";
    } else {
      tempoEl.textContent = "Tempo: —";
    }
    draw();
  }

  function detectAndApply() {
    if (!buffer) return;
    const cached = tempoCache.get(audioHash);
    if (cached) { applyTempo(cached); return; }
    // Defer so the first waveform paint is not blocked by analysis.
    setTimeout(() => {
      if (destroyed || !buffer) return;
      let result;
      try { result = detectTempo(buffer); }
      catch { result = { bpm: 0, firstBeatSec: 0, confidence: 0 }; }
      tempoCache.set(audioHash, result);
      if (!destroyed) applyTempo(result);
    }, 0);
  }

  // ---- listeners ----
  canvas.addEventListener("pointerdown", onPointerDown);
  canvas.addEventListener("pointermove", onPointerMove);
  canvas.addEventListener("pointerup", onPointerUp);
  canvas.addEventListener("pointercancel", onPointerUp);
  canvas.addEventListener("wheel", onWheel, { passive: false });
  toolbar.addEventListener("click", onToolbarClick);
  thumb.addEventListener("pointerdown", onThumbDown);
  thumb.addEventListener("pointermove", onThumbMove);
  thumb.addEventListener("pointerup", onThumbUp);
  thumb.addEventListener("pointercancel", onThumbUp);
  document.addEventListener("keydown", onKeyDown);

  const ro = new ResizeObserver(() => { measureCanvas(); draw(); syncScrollbar(); });
  ro.observe(canvas);

  getAudioBuffer(audioHash)
    .then((buf) => {
      if (destroyed) return;
      buffer = buf;
      status.remove();
      view = fitView(duration, MIN_VIEW_DUR);
      peaks = null;
      invalidateStatic();
      draw();
      syncScrollbar();
      detectAndApply();
    })
    .catch(() => {
      if (destroyed) return;
      status.textContent = "Waveform unavailable";
    });

  measureCanvas();
  draw();
  syncScrollbar();

  // External sync (e.g. after a server snapshot echoes back the trim we just
  // sent) -- ignored mid-drag so it can't fight the user's pointer.
  function setTrim(newIn, newOut) {
    if (dragging) return;
    const nextIn = clamp(Number(newIn) || 0, 0, duration);
    const nextOut = clamp(newOut && Number(newOut) > 0 ? Number(newOut) : duration, 0, duration);
    // Called on every 15 Hz server-state tick; skip the redraw when the trim is
    // unchanged so an idle widget does not repaint the whole canvas.
    if (nextIn === trimIn && nextOut === trimOut) return;
    trimIn = nextIn;
    trimOut = nextOut;
    invalidateStatic();   // trim window shapes the envelope-scaled peaks
    draw();
  }

  // External sync of the fade lengths + curve shape (mirrors setTrim). Ignored
  // mid-drag so an echoed server snapshot can't fight the pointer. Amendment 2:
  // `shape` is mandatory -- a shape change re-strokes even if the two lengths
  // are unchanged, so the envelope uses the right curve.
  function setFades(newIn, newOut, shape) {
    if (dragging) return;
    const nextIn = Math.max(0, Number(newIn) || 0);
    const nextOut = Math.max(0, Number(newOut) || 0);
    const nextShape = shape === "equalPower" ? "equalPower" : "linear";
    if (nextIn === fadeIn && nextOut === fadeOut && nextShape === fadeShape) return;
    fadeIn = nextIn;
    fadeOut = nextOut;
    fadeShape = nextShape;
    invalidateStatic();
    draw();
  }

  // Start/stop the playhead marker loop. Idempotent. Stopping redraws once to
  // clear the marker.
  function setPlaying(on) {
    on = !!on;
    if (on === playing) return;
    playing = on;
    if (on) {
      if (rafId == null) rafId = requestAnimationFrame(tick);
    } else {
      if (rafId != null) { cancelAnimationFrame(rafId); rafId = null; }
      draw();
    }
  }

  function destroy() {
    destroyed = true;
    playing = false;
    if (rafId != null) { cancelAnimationFrame(rafId); rafId = null; }
    ro.disconnect();
    canvas.removeEventListener("pointerdown", onPointerDown);
    canvas.removeEventListener("pointermove", onPointerMove);
    canvas.removeEventListener("pointerup", onPointerUp);
    canvas.removeEventListener("pointercancel", onPointerUp);
    canvas.removeEventListener("wheel", onWheel);
    toolbar.removeEventListener("click", onToolbarClick);
    thumb.removeEventListener("pointerdown", onThumbDown);
    thumb.removeEventListener("pointermove", onThumbMove);
    thumb.removeEventListener("pointerup", onThumbUp);
    thumb.removeEventListener("pointercancel", onThumbUp);
    document.removeEventListener("keydown", onKeyDown);
    container.innerHTML = "";
  }

  return { destroy, setTrim, setPlaying, setFades };
}
