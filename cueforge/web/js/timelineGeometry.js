// Pure geometry for the compound-cue timeline editor: all time<->px, snapping,
// clip span/rect and pointer hit-zone math. No DOM here so this stays
// unit-testable under Node and mirrors waveformView.js conventions.
//
// A "view" is { start, dur } in seconds: start = time at the left edge of a
// lane, dur = seconds visible across the lane's inner pixel width. Unlike the
// waveform view there is no edge gutter -- lanes are plain horizontally
// scrollable strips, so px map straight from the lane's left edge.

// Px zone at each clip edge that grabs a trim handle.
export const EDGE_PX = 8;
// Px zone at the top corners of a clip that grabs a fade handle.
export const FADE_PX = 10;
// Default snap grid (seconds).
export const SNAP_SECONDS = 0.1;

// "Nice" ruler tick intervals (seconds), smallest -> largest (matches
// waveformView.js so both rulers pick the same friendly numbers).
const NICE = [0.1, 0.25, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300];

function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

export function pxPerSec(view, width) {
  return width / (view.dur > 0 ? view.dur : 1);
}

export function timeToX(t, view, width) {
  return (t - view.start) * pxPerSec(view, width);
}

export function xToTime(x, view, width) {
  return view.start + x / pxPerSec(view, width);
}

// Round a time to the snap grid. grid <= 0 disables snapping.
export function snap(t, grid = SNAP_SECONDS) {
  return grid > 0 ? Math.round(t / grid) * grid : t;
}

// Nearest value in `candidates` (seconds) within `threshold` of t, else t
// unchanged. Pure: no DOM, no store. `threshold` is seconds (caller passes
// EDGE_PX / zoom). Ties resolve to the later candidate.
export function snapValue(t, candidates, threshold) {
  let best = t, bestD = threshold;
  for (let i = 0; i < candidates.length; i++) {
    const d = Math.abs(candidates[i] - t);
    if (d <= bestD) { bestD = d; best = candidates[i]; }   // ties -> later candidate
  }
  return best;
}

// Edge-priority snap: a nearby clip edge (within `threshold` of the RAW,
// un-gridded time) beats the grid; the grid applies only when no edge is
// close. Testing candidates against the gridded value instead would let the
// grid round *past* the threshold and starve edge snapping whenever the grid
// step >= threshold.
export function snapWithEdges(raw, grid, candidates, threshold) {
  const edged = snapValue(raw, candidates, threshold);
  return edged !== raw ? edged : snap(raw, grid);
}

// Keep the window inside [0, contentDur]; dur inside [minDur, contentDur].
// Mirrors waveformView.clampView (contentDur is the timeline's total length).
export function clampView(view, contentDur, minDur) {
  const durCap = contentDur > 0 ? contentDur : minDur;
  const dur = clamp(view.dur, minDur, durCap);
  const start = clamp(view.start, 0, Math.max(0, durCap - dur));
  return { start, dur };
}

// Multiply dur by factor while keeping anchorT fixed at its current pixel.
export function zoomAbout(view, factor, anchorT, contentDur, minDur) {
  const rel = view.dur > 0 ? (anchorT - view.start) / view.dur : 0;
  const newDur = view.dur * factor;
  return clampView({ start: anchorT - rel * newDur, dur: newDur }, contentDur, minDur);
}

export function fitView(contentDur, minDur) {
  return { start: 0, dur: Math.max(minDur, contentDur || minDur) };
}

// Effective source-end (seconds) for a clip, honoring clipOut === null (or
// undefined) meaning "play to the source's end".
export function clipOutSeconds(clip, sourceDur) {
  return clip.clipOut == null ? sourceDur : clip.clipOut;
}

// Playable span of a clip on its lane (seconds).
export function clipSpan(clip, sourceDur) {
  const inS = Math.max(0, clip.clipIn || 0);
  const outS = clipOutSeconds(clip, sourceDur);
  return Math.max(0, outS - inS);
}

// Pixel {x, w} of a clip within its lane for the current view.
export function clipRect(clip, sourceDur, view, width) {
  const x = timeToX(clip.start || 0, view, width);
  const w = clipSpan(clip, sourceDur) * pxPerSec(view, width);
  return { x, w };
}

// Which interactive zone a pointer at localX (px from the clip's left edge),
// localY (px from the clip's top) hits. Order matters: fade handles win over
// trim edges so the top band always grabs a fade handle near its actual dot.
//
// The fade grab is anchored to each fade handle's real on-screen x (fadeInPx
// from the left, clipW - fadeOutPx from the right): a pointer within FADE_PX of
// a handle in the top band grabs that fade. The trim edge shrinks proportionally
// on tiny clips (min(EDGE_PX, clipW/3)) so a draggable body zone always survives
// even on a sub-EDGE_PX clip (spec C6). Pure: safe to unit test.
export function clipZone(localX, localY, clipW, fadeInPx = 0, fadeOutPx = 0, headerH = 0) {
  const edge = Math.min(EDGE_PX, clipW / 3);
  // Top FADE band: grab within ~FADE_PX of the handle at its actual x (fade wins).
  if (localY >= headerH && localY < headerH + FADE_PX) {
    if (Math.abs(localX - fadeInPx) <= FADE_PX) return "fadeIn";
    if (Math.abs(localX - (clipW - fadeOutPx)) <= FADE_PX) return "fadeOut";
  }
  if (localX < edge) return "trimStart";
  if (localX > clipW - edge) return "trimEnd";
  return "body";
}

// Resolve a desired start (seconds) for a clip of `span` on one track so its
// span [start, start+span) never overlaps any other clip on that track. Pure:
// takes a plain [{id, start, span}] array (excludeId skips the moving clip) and
// returns the nearest free start >= 0. Because it picks the free gap whose
// clamped position is closest to `desired`, a dragged clip "jumps over" a
// neighbour exactly as its centre crosses the neighbour's centre. Terminates in
// a single sorted scan -- no unbounded cascade.
export function resolveNoOverlap(trackClips, excludeId, desired, span) {
  span = Math.max(0, span || 0);
  const d = Math.max(0, desired || 0);
  const iv = [];
  for (const c of trackClips) {
    if (c.id === excludeId) continue;
    const s = Math.max(0, c.start || 0);
    const e = s + Math.max(0, c.span || 0);
    if (e > s) iv.push([s, e]);
  }
  if (!iv.length) return d;
  iv.sort((a, b) => a[0] - b[0]);
  // Merge overlapping/touching occupied intervals.
  const merged = [];
  for (const seg of iv) {
    const top = merged[merged.length - 1];
    if (top && seg[0] <= top[1] + 1e-9) top[1] = Math.max(top[1], seg[1]);
    else merged.push([seg[0], seg[1]]);
  }
  // Free gaps between occupied blocks (last gap is open-ended).
  const gaps = [];
  let prev = 0;
  for (const [s, e] of merged) {
    if (s - prev >= span - 1e-9) gaps.push([prev, s]);
    prev = Math.max(prev, e);
  }
  gaps.push([prev, Infinity]);
  let best = d, bestDist = Infinity;
  for (const [gs, ge] of gaps) {
    const hi = ge === Infinity ? Infinity : ge - span;
    if (hi !== Infinity && hi < gs - 1e-9) continue;   // gap cannot hold the span
    let c = d;
    if (c < gs) c = gs;
    if (hi !== Infinity && c > hi) c = hi;
    const dist = Math.abs(c - d);
    if (dist < bestDist) { bestDist = dist; best = c; }
  }
  return Math.max(0, best);
}

// Smallest nice interval whose on-screen spacing is >= minPx.
export function chooseTickInterval(view, width, minPx = 80) {
  const pps = pxPerSec(view, width);
  for (const iv of NICE) if (iv * pps >= minPx) return iv;
  return NICE[NICE.length - 1];
}
