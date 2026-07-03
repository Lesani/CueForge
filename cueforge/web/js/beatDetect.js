// In-house automatic tempo (BPM) + downbeat estimator. Pure math on Float32
// samples so it is unit-testable under Node (no WebAudio/DOM). Assumes a
// roughly constant 4/4 tempo. Returns { bpm, firstBeatSec, confidence } with
// confidence in [0,1]; bpm 0 / confidence 0 when it cannot tell.

const TARGET_SR = 11025;   // analysis sample rate
const HOP = 256;           // onset-envelope hop (samples @ TARGET_SR)
const FRAME = 512;         // onset-envelope frame size
const BPM_MIN = 70;
const BPM_MAX = 180;
const BEATS_PER_BAR = 4;

// Average-decimate mono samples down to ~TARGET_SR.
function downsample(samples, sampleRate) {
  const factor = Math.max(1, Math.round(sampleRate / TARGET_SR));
  if (factor === 1) return { data: samples, sr: sampleRate };
  const outN = Math.floor(samples.length / factor);
  const out = new Float32Array(outN);
  for (let i = 0; i < outN; i++) {
    let s = 0;
    const base = i * factor;
    for (let k = 0; k < factor; k++) s += samples[base + k];
    out[i] = s / factor;
  }
  return { data: out, sr: sampleRate / factor };
}

// Half-wave-rectified energy flux -> onset strength per hop.
function onsetEnvelope(data) {
  const frames = Math.max(0, 1 + Math.floor((data.length - FRAME) / HOP));
  if (frames < 4) return new Float32Array(0);
  const env = new Float32Array(frames);
  let prev = 0;
  for (let f = 0; f < frames; f++) {
    const start = f * HOP;
    let e = 0;
    for (let i = 0; i < FRAME; i++) { const v = data[start + i]; e += v * v; }
    const flux = e - prev;
    env[f] = flux > 0 ? flux : 0;
    prev = e;
  }
  // Normalize to unit mean so confidence is scale-independent.
  let mean = 0;
  for (let i = 0; i < env.length; i++) mean += env[i];
  mean /= env.length || 1;
  if (mean > 0) for (let i = 0; i < env.length; i++) env[i] /= mean;
  return env;
}

// Parabolic peak interpolation: returns fractional offset in [-0.5,0.5].
function parabolicOffset(ym1, y0, yp1) {
  const denom = ym1 - 2 * y0 + yp1;
  if (denom === 0) return 0;
  return (0.5 * (ym1 - yp1)) / denom;
}

export function detectTempoFromSamples(samples, sampleRate) {
  const unknown = { bpm: 0, firstBeatSec: 0, confidence: 0 };
  if (!samples || samples.length < sampleRate) return unknown; // < ~1s: give up

  const { data, sr } = downsample(samples, sampleRate);
  const env = onsetEnvelope(data);
  if (env.length < 8) return unknown;
  const fps = sr / HOP; // onset frames per second

  const lagMin = Math.max(2, Math.floor((60 / BPM_MAX) * fps));
  const lagMax = Math.min(env.length - 1, Math.ceil((60 / BPM_MIN) * fps));
  if (lagMax <= lagMin) return unknown;

  // Autocorrelation of the onset envelope over the candidate lag range.
  const ac = new Float32Array(lagMax + 1);
  let acSum = 0;
  for (let lag = lagMin; lag <= lagMax; lag++) {
    let s = 0;
    for (let i = lag; i < env.length; i++) s += env[i] * env[i - lag];
    ac[lag] = s / (env.length - lag);
    acSum += ac[lag];
  }
  const acMean = acSum / (lagMax - lagMin + 1);

  // Strongest periodicity.
  let bestLag = lagMin, bestVal = -Infinity;
  for (let lag = lagMin; lag <= lagMax; lag++) {
    if (ac[lag] > bestVal) { bestVal = ac[lag]; bestLag = lag; }
  }
  // Sub-frame refinement.
  let refined = bestLag;
  if (bestLag > lagMin && bestLag < lagMax) {
    refined += parabolicOffset(ac[bestLag - 1], ac[bestLag], ac[bestLag + 1]);
  }
  const bpm = (60 * fps) / refined;
  const confidence = bestVal > 0 ? Math.max(0, Math.min(1, (bestVal - acMean) / bestVal)) : 0;

  // Beat phase: best offset in [0, bestLag) for a pulse train at bestLag.
  let bestPhase = 0, bestPhaseVal = -Infinity;
  for (let off = 0; off < bestLag; off++) {
    let s = 0;
    for (let i = off; i < env.length; i += bestLag) s += env[i];
    if (s > bestPhaseVal) { bestPhaseVal = s; bestPhase = off; }
  }
  // Downbeat: which of the 4 beats in a bar carries the most energy.
  let bestBeat = 0, bestBeatVal = -Infinity;
  for (let b = 0; b < BEATS_PER_BAR; b++) {
    let s = 0;
    for (let i = bestPhase + b * bestLag; i < env.length; i += bestLag * BEATS_PER_BAR) s += env[i];
    if (s > bestBeatVal) { bestBeatVal = s; bestBeat = b; }
  }
  const firstBeatFrame = bestPhase + bestBeat * bestLag;
  let firstBeatSec = firstBeatFrame / fps;
  const barDur = (60 / bpm) * BEATS_PER_BAR;
  if (barDur > 0) firstBeatSec = ((firstBeatSec % barDur) + barDur) % barDur;

  return { bpm, firstBeatSec, confidence };
}

export function detectTempo(audioBuffer) {
  if (!audioBuffer || !audioBuffer.length) return { bpm: 0, firstBeatSec: 0, confidence: 0 };
  const n = audioBuffer.length;
  const chs = audioBuffer.numberOfChannels || 1;
  const mono = new Float32Array(n);
  for (let c = 0; c < chs; c++) {
    const d = audioBuffer.getChannelData(c);
    for (let i = 0; i < n; i++) mono[i] += d[i];
  }
  if (chs > 1) for (let i = 0; i < n; i++) mono[i] /= chs;
  return detectTempoFromSamples(mono, audioBuffer.sampleRate);
}
