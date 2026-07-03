// Pure geometry for the zoomable/scrollable waveform view window.
// A "view" is { start, dur } in seconds: start = time at the left gutter edge,
// dur = seconds visible across the inner (padded) canvas width. No DOM here so
// this stays unit-testable under Node.

// Horizontal gutter (px) reserved on each edge so edge handles stay grabbable.
export const PAD = 12;

// "Nice" ruler tick intervals (seconds), smallest -> largest.
const NICE = [0.1, 0.25, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300];

function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

export function innerWidth(width) { return Math.max(1, width - 2 * PAD); }

export function pxPerSec(view, width) {
  const d = view.dur > 0 ? view.dur : 1;
  return innerWidth(width) / d;
}

export function timeToX(t, view, width) {
  return PAD + (t - view.start) * pxPerSec(view, width);
}

export function xToTime(x, view, width) {
  return view.start + (x - PAD) / pxPerSec(view, width);
}

// Keep the window inside [0, duration]; dur inside [minDur, duration].
export function clampView(view, duration, minDur) {
  const durCap = duration > 0 ? duration : minDur;
  const dur = clamp(view.dur, minDur, durCap);
  const start = clamp(view.start, 0, Math.max(0, durCap - dur));
  return { start, dur };
}

// Multiply dur by factor while keeping anchorT fixed at its current pixel.
export function zoomAbout(view, factor, anchorT, duration, minDur) {
  const rel = view.dur > 0 ? (anchorT - view.start) / view.dur : 0;
  const newDur = view.dur * factor;
  return clampView({ start: anchorT - rel * newDur, dur: newDur }, duration, minDur);
}

export function fitView(duration, minDur) {
  return { start: 0, dur: Math.max(minDur, duration || minDur) };
}

export function selectionView(t0, t1, duration, minDur) {
  const a = Math.min(t0, t1), b = Math.max(t0, t1);
  return clampView({ start: a, dur: Math.max(minDur, b - a) }, duration, minDur);
}

// Smallest nice interval whose on-screen spacing is >= minPx.
export function chooseTickInterval(view, width, minPx = 80) {
  const pps = pxPerSec(view, width);
  for (const iv of NICE) if (iv * pps >= minPx) return iv;
  return NICE[NICE.length - 1];
}

// Bar (Takt) start times across [0, duration], numbered from the first downbeat.
export function barTimes(bpm, firstBeatSec, duration, beatsPerBar = 4) {
  if (!(bpm > 0) || !(duration > 0)) return [];
  const barDur = (60 / bpm) * beatsPerBar;
  if (!(barDur > 0)) return [];
  const out = [];
  let n = 1;
  for (let t = firstBeatSec; t <= duration + 1e-9; t += barDur) {
    if (t >= -1e-9) out.push({ time: t, number: n });
    n++;
    if (out.length > 100000) break; // safety
  }
  return out;
}
