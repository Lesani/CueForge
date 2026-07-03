// CueForge shared helpers.

// Engine sample rate (48 kHz). frame counts -> seconds.
export const SAMPLE_RATE = 48000;

// A figure-space: same advance width as a digit in tabular-figure fonts, used
// to reserve the sign column so the timer never shifts horizontally.
const SIGN_BLANK = " ";

/**
 * Fixed-width remaining-time string for the big bottom timer.
 * `remaining` is seconds (>= 0). Returns e.g. "-01:23" while counting down,
 * or " 00:00" (blank reserved sign slot) when nothing is playing.
 * Assumes a monospace / tabular-figure font so width stays constant.
 */
export function formatTimer(remaining) {
  if (!(remaining > 0)) remaining = 0;
  const sign = remaining > 0 ? "-" : SIGN_BLANK;
  const total = Math.floor(remaining + 1e-6);
  const mm = String(Math.floor(total / 60)).padStart(2, "0");
  const ss = String(total % 60).padStart(2, "0");
  return sign + mm + ":" + ss;
}

/**
 * Compact clock for cell durations / running times: "M:SS" (or "MM:SS" past
 * ten minutes). Seconds are zero-padded; minutes are not.
 */
export function formatClock(seconds) {
  if (!(seconds > 0)) seconds = 0;
  const total = Math.floor(seconds + 1e-6);
  const m = Math.floor(total / 60);
  const s = String(total % 60).padStart(2, "0");
  return m + ":" + s;
}

// Convert engine frames to seconds.
export function framesToSeconds(frames) {
  return (frames || 0) / SAMPLE_RATE;
}

// Intrinsic playable duration (seconds) of a library item: the trim window,
// falling back to the full decoded `duration` when trimOut is unset.
// Returns null when unknown (e.g. stop cues carry no audio).
export function itemDuration(item) {
  if (!item) return null;
  const inS = Number(item.trimIn) || 0;
  const outS = Number(item.trimOut);
  const end = outS && outS > 0 ? outS : (Number(item.duration) || 0);
  if (end > inS) return end - inS;
  return null;
}

// Escape text for safe insertion into innerHTML.
export function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// True when focus is in a text-entry control (suppress global keyboard).
export function isTypingTarget(el) {
  if (!el) return false;
  const tag = el.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" ||
         el.isContentEditable === true;
}
