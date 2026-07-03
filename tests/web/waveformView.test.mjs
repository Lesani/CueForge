import { test } from "node:test";
import assert from "node:assert/strict";
import {
  PAD, pxPerSec, timeToX, xToTime, clampView, zoomAbout,
  fitView, selectionView, chooseTickInterval, barTimes,
} from "../../cueforge/web/js/waveformView.js";

const W = 512; // canvas css width
const inner = W - 2 * PAD;

test("timeToX / xToTime are inverse over the view", () => {
  const view = { start: 2, dur: 4 };
  const x = timeToX(3.5, view, W);
  assert.ok(Math.abs(xToTime(x, view, W) - 3.5) < 1e-9);
  // start maps to PAD, end maps to PAD+inner
  assert.ok(Math.abs(timeToX(2, view, W) - PAD) < 1e-9);
  assert.ok(Math.abs(timeToX(6, view, W) - (PAD + inner)) < 1e-9);
});

test("pxPerSec scales with zoom", () => {
  assert.ok(pxPerSec({ start: 0, dur: 4 }, W) > pxPerSec({ start: 0, dur: 8 }, W));
});

test("clampView keeps the window inside [0, duration]", () => {
  assert.deepEqual(clampView({ start: -5, dur: 4 }, 10, 0.05), { start: 0, dur: 4 });
  assert.deepEqual(clampView({ start: 100, dur: 4 }, 10, 0.05), { start: 6, dur: 4 });
  // dur clamps to duration
  assert.deepEqual(clampView({ start: 0, dur: 999 }, 10, 0.05), { start: 0, dur: 10 });
  // dur clamps to minDur
  assert.equal(clampView({ start: 0, dur: 0.001 }, 10, 0.05).dur, 0.05);
});

test("fitView spans the whole clip", () => {
  assert.deepEqual(fitView(10, 0.05), { start: 0, dur: 10 });
});

test("zoomAbout keeps the anchor time at the same pixel", () => {
  const view = { start: 0, dur: 10 };
  const anchor = 4;
  const xBefore = timeToX(anchor, view, W);
  const zoomed = zoomAbout(view, 0.5, anchor, 10, 0.05);
  const xAfter = timeToX(anchor, zoomed, W);
  assert.ok(zoomed.dur < view.dur);
  assert.ok(Math.abs(xAfter - xBefore) < 1e-6);
});

test("selectionView snaps to the dragged range (order-independent)", () => {
  assert.deepEqual(selectionView(6, 2, 10, 0.05), { start: 2, dur: 4 });
});

test("chooseTickInterval grows the spacing as we zoom out", () => {
  const zoomedIn = chooseTickInterval({ start: 0, dur: 2 }, W, 80);
  const zoomedOut = chooseTickInterval({ start: 0, dur: 600 }, W, 80);
  assert.ok(zoomedOut > zoomedIn);
});

test("barTimes numbers Takte from the first downbeat", () => {
  const bars = barTimes(120, 0.5, 5, 4); // barDur = 2s
  assert.deepEqual(bars[0], { time: 0.5, number: 1 });
  assert.deepEqual(bars[1], { time: 2.5, number: 2 });
  assert.equal(bars.at(-1).time <= 5 + 1e-9, true);
  assert.deepEqual(barTimes(0, 0.5, 5), []);
  assert.deepEqual(barTimes(120, 0.5, 0), []);
});
