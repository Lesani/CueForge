// CueForge compound-cue timeline preview engine. A deep, DOM-free module that
// owns the WebAudio scheduling + the RAF clock for client-side preview of a
// compound timeline. timeline.js owns the `tl` model and source lookups and
// feeds this engine plain PreviewClip descriptors, so the engine never imports
// store. It approximates the offline renderer's clip gain + fade envelope
// (linear -> linearRamp, equalPower -> setValueCurveAtTime); see ADR 0005 for
// the approximate-preview contract (overlapping fades diverge slightly).

import { getAudioContext, getAudioBuffer } from "./audioCache.js";

const HALF_PI = Math.PI / 2;
const LOOKAHEAD = 0.03;          // s: schedule slightly ahead so setValueAtTime(when) is future
const EQP_SAMPLES = 64;          // equal-power curve resolution over a full span
function dbToGain(db) { return Math.pow(10, (db || 0) / 20); }

// createTimelinePreview({ getClips, getContentEnd, onFrame, onState }) -> handle
//   getClips()      -> Array<PreviewClip>  (built by timeline.js; mute/missing/zero-span already filtered)
//   getContentEnd() -> number seconds (content end; recomputed on each play/rebuild)
//   onFrame(t)      -> called each RAF while playing AND once on a paused seek
//   onState(playing)-> called on every play/pause/stop transition
//
// PreviewClip = { hash, start, span, clipIn, gainDb, fadeIn, fadeOut, fadeShape }
export function createTimelinePreview({ getClips, getContentEnd, onFrame, onState }) {
  let ctx = null, master = null;
  let nodes = [];                // [{src, gain}] live voices, for teardown
  let playing = false, destroyed = false;
  let anchorCtx = 0, anchorT = 0, endT = 0, rafId = 0;
  let playGen = 0;               // guards the async play() against interleaving

  function currentTime() {
    if (!playing || !ctx) return anchorT;
    return Math.max(anchorT, anchorT + (ctx.currentTime - anchorCtx));
  }

  async function play(t0) {
    if (destroyed) return;
    const gen = ++playGen;
    const start = (t0 == null) ? currentTime() : Math.max(0, t0);
    // Nothing to play from here (empty timeline, or seek past the content end)?
    // Bail BEFORE flipping state so the Play button never flickers to "playing"
    // then straight back. Leave the anchor where it is.
    if (getContentEnd() <= start + 1e-9) { anchorT = start; return; }
    teardown();                                   // idempotent
    const clips = getClips();
    // resolve buffers (cached => resolve on the microtask queue)
    await Promise.all(clips.map((c) =>
      getAudioBuffer(c.hash).then((b) => { c.buffer = b; }).catch(() => { c.buffer = null; })));
    if (destroyed || gen !== playGen) return;     // superseded while awaiting
    ctx = getAudioContext();                       // resumes on the user gesture
    if (ctx.state === "suspended") { try { ctx.resume(); } catch {} }
    master = ctx.createGain(); master.gain.value = 1; master.connect(ctx.destination);
    anchorT = start;
    anchorCtx = ctx.currentTime + LOOKAHEAD;       // ctx time that maps to timeline `start`
    for (const c of clips) if (c.buffer) scheduleClip(c, start, anchorCtx);
    endT = getContentEnd();
    playing = true; onState(true);
    cancelAnimationFrame(rafId); rafId = requestAnimationFrame(tick);
  }

  function tick() {
    if (!playing) return;
    const t = currentTime();
    if (t >= endT - 1e-3) { anchorT = endT; teardown(); playing = false; onState(false); onFrame(endT); return; }
    onFrame(t);
    rafId = requestAnimationFrame(tick);
  }

  function pause() {
    if (!playing) return;
    playGen++;                    // cancel any pending play() awaiting buffers
    anchorT = currentTime(); teardown(); playing = false;
    cancelAnimationFrame(rafId); onState(false);
  }
  function toggle(t) { if (playing) pause(); else play(t); }
  function seek(t) { const s = Math.max(0, t); if (playing) play(s); else { anchorT = s; onFrame(s); } }
  function rebuild() { if (playing) play(currentTime()); }

  function teardown() {
    for (const n of nodes) { try { n.src.stop(); } catch {} try { n.src.disconnect(); } catch {} try { n.gain.disconnect(); } catch {} }
    nodes = [];
    if (master) { try { master.disconnect(); } catch {} master = null; }
  }
  function destroy() { destroyed = true; teardown(); cancelAnimationFrame(rafId); playing = false; }

  function scheduleClip(clip, t0, startAt) {
    const span = clip.span;
    const clipEnd = clip.start + span;
    if (clipEnd <= t0) return;                       // finished before the seek point
    const tau0 = Math.max(0, t0 - clip.start);       // seconds already elapsed into this clip
    const when = startAt + Math.max(0, clip.start - t0);
    const playDur = span - tau0;
    if (playDur <= 0) return;
    const src = ctx.createBufferSource();
    src.buffer = clip.buffer;
    const g = ctx.createGain();
    applyEnvelope(g.gain, clip, span, tau0, when, playDur);
    src.connect(g); g.connect(master);
    src.start(when, (clip.clipIn || 0) + tau0, playDur);
    src.stop(when + playDur);
    nodes.push({ src, gain: g });
  }

  function applyEnvelope(param, clip, span, tau0, when, playDur) {
    const base = dbToGain(clip.gainDb);
    let fi = Math.min(Math.max(0, clip.fadeIn || 0), span);
    let fo = Math.min(Math.max(0, clip.fadeOut || 0), span);
    if (fi + fo > span && fi + fo > 0) {             // preview clamp: proportional, no overlap
      const k = span / (fi + fo); fi *= k; fo *= k;
    }
    const foStart = span - fo;                       // clip-local time the fade-out begins
    const eqp = clip.fadeShape === "equalPower";
    const envAt = (tau) => {
      let m = 1;
      if (fi > 0 && tau < fi)      m *= eqp ? Math.sin((tau / fi) * HALF_PI) : (tau / fi);
      if (fo > 0 && tau > foStart) { const p = (tau - foStart) / fo; m *= eqp ? Math.cos(p * HALF_PI) : (1 - p); }
      return m;
    };
    param.cancelScheduledValues(when);
    if (fi === 0 && fo === 0) { param.setValueAtTime(base, when); return; }
    if (!eqp) {
      param.setValueAtTime(base * envAt(tau0), when);
      if (fi > 0 && tau0 < fi) param.linearRampToValueAtTime(base, when + (fi - tau0));
      if (fo > 0) {
        const foRel = foStart - tau0;
        if (foRel > 0) param.setValueAtTime(base, when + foRel);
        param.linearRampToValueAtTime(0, when + (span - tau0));
      }
    } else {
      const N = Math.max(2, Math.round(EQP_SAMPLES * (playDur / span)));
      const curve = new Float32Array(N);
      for (let i = 0; i < N; i++) curve[i] = base * envAt(tau0 + (playDur * i) / (N - 1));
      param.setValueCurveAtTime(curve, when, playDur);   // own GainNode => no automation overlap
    }
  }

  return { toggle, play, pause, seek, rebuild, isPlaying: () => playing, currentTime, destroy };
}
