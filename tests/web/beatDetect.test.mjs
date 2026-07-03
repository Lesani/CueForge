import { test } from "node:test";
import assert from "node:assert/strict";
import { detectTempoFromSamples } from "../../cueforge/web/js/beatDetect.js";

// Build a mono click track: short decaying impulses every (60/bpm) seconds.
function clickTrack(bpm, seconds, sampleRate) {
  const n = Math.floor(seconds * sampleRate);
  const buf = new Float32Array(n);
  const period = Math.round((60 / bpm) * sampleRate);
  for (let i = 0; i < n; i += period) {
    for (let k = 0; k < 200 && i + k < n; k++) {
      buf[i + k] += Math.exp(-k / 40) * (k % 2 ? -1 : 1); // decaying ping
    }
  }
  return buf;
}

function noise(seconds, sampleRate, seed = 1) {
  const n = Math.floor(seconds * sampleRate);
  const buf = new Float32Array(n);
  let s = seed;
  for (let i = 0; i < n; i++) { s = (s * 1103515245 + 12345) & 0x7fffffff; buf[i] = (s / 0x7fffffff) * 2 - 1; }
  return buf;
}

test("detects 120 BPM within tolerance and reports high confidence", () => {
  const r = detectTempoFromSamples(clickTrack(120, 12, 44100), 44100);
  assert.ok(Math.abs(r.bpm - 120) <= 2, `bpm=${r.bpm}`);
  assert.ok(r.confidence >= 0.5, `confidence=${r.confidence}`);
  assert.ok(isFinite(r.firstBeatSec) && r.firstBeatSec >= 0);
});

test("detects 90 BPM within tolerance", () => {
  const r = detectTempoFromSamples(clickTrack(90, 12, 44100), 44100);
  assert.ok(Math.abs(r.bpm - 90) <= 2, `bpm=${r.bpm}`);
});

test("white noise yields low confidence", () => {
  const r = detectTempoFromSamples(noise(12, 44100), 44100);
  assert.ok(r.confidence < 0.5, `confidence=${r.confidence}`);
});

test("too-short / empty input is unknown, not a throw", () => {
  const r = detectTempoFromSamples(new Float32Array(10), 44100);
  assert.equal(r.confidence, 0);
  assert.equal(r.bpm, 0);
});
