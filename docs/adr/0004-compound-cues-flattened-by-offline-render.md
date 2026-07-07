# 4. Compound cues are flattened by an offline render into ordinary audio

Date: 2026-07-06

## Status

Accepted

## Context

A Compound Cue is assembled from other library sounds on a multi-track
timeline (clips with offset, trim, gain, fades) but must fire as ONE entry in
the grid. Two ways to play it:

1. **Live-mix the timeline at fire time** — the engine would schedule every
   clip as its own voice. Sample-accurate by construction and always
   up-to-date, but: multiplies live voice count unpredictably, makes the
   compound interact with the voice cap / pause / live-gain / stop semantics
   as N voices instead of one, and every future timeline feature (effects,
   speed) becomes a *realtime* engine feature.
2. **Flatten offline** — a background renderer mixes the timeline into a
   single stereo blob stored like imported audio; the grid plays that blob.

## Decision

Option 2, with one sharpening: **the rendered result is stored as the item's
regular `audio_hash`**. Firing, audition, waveforms, export, duration, trim /
fades / loop and Output routing of a compound item therefore reuse the
existing code paths byte-for-byte — a compound is indistinguishable from an
imported sound outside the editor. Extra fields carry only editor/render
state: `timeline`, `render_signature` (hash of timeline + source audio
hashes), `render_state`, `render_error`.

A show always fires the **last completed render**, even if the timeline has
unrendered edits (predictable output mid-performance beats freshness).
Renders run debounced in a background thread after edits and on project open
when the signature mismatches.

Clips reference their source by **library item id** (so names and re-imports
follow the library); a deleted source renders as silence and the editor marks
the clip missing. The clip schema carries an empty `effects: []` list purely
as forward-compatibility for later DSP.

## Consequences

- One voice at fire time: compounds compose cleanly with chains, pause,
  fades, PANIC and the voice cap. Zero added realtime cost.
- Rendering is not free: after an edit the audible result lags by the render
  time (seconds for long timelines); the UI must show render state honestly.
- Offline-only DSP: future effects only need a numpy implementation, never a
  realtime one.
- Storage grows: source audio AND flattened render are both kept (FLAC,
  content-addressed — identical re-renders dedupe to the same hash).
- Editing a *source* item's audio does not silently change an existing
  render until the compound's signature check notices (source hash is part
  of the signature), keeping renders reproducible.
