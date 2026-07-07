// Fade envelope multiplier in [0,1] for clip-local time `tau` seconds within a
// clip/region of `span` seconds. Mirrors timelinePreview.applyEnvelope exactly:
// proportional clamp when fadeIn+fadeOut exceed the span. Pure; no DOM.
//
// This is the single source of truth for the VISUAL fade envelope shared by all
// three waveform draw paths (timeline clip wave, library trim widget, library
// compound wave). It must stay in sync with timelinePreview.applyEnvelope, which
// owns the AUDIO path (WebAudio automation) and is deliberately kept separate to
// avoid coupling a pure display helper to the audio-scheduling risk surface.
const HALF_PI = Math.PI / 2;

export function envAt(tau, fadeIn, fadeOut, span, shape) {
  if (!(span > 0)) return 1;
  let fi = Math.min(Math.max(0, fadeIn || 0), span);
  let fo = Math.min(Math.max(0, fadeOut || 0), span);
  if (fi + fo > span && fi + fo > 0) { const k = span / (fi + fo); fi *= k; fo *= k; }
  const foStart = span - fo;
  const eqp = shape === "equalPower";
  let m = 1;
  if (fi > 0 && tau < fi)        m *= eqp ? Math.sin((tau / fi) * HALF_PI) : (tau / fi);
  if (fo > 0 && tau > foStart) { const p = (tau - foStart) / fo; m *= eqp ? Math.cos(p * HALF_PI) : (1 - p); }
  return Math.max(0, Math.min(1, m));
}
