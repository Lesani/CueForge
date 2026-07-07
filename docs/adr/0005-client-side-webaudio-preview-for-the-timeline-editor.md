# 5. Client-side WebAudio preview for the timeline editor

Date: 2026-07-06

## Status

Accepted

## Context

The compound-cue timeline editor needs play/pause/seek with a moving playhead
— without it, non-technical users cannot hear what they are building. Two
candidate architectures:

1. **Server audition of the rendered blob**: bit-exact and routed through the
   real Outputs, but only available after a render completes, cannot seek,
   and every edit invalidates it for seconds (debounce + render latency).
2. **Client-side WebAudio preview**: the editor already holds the decoded
   source buffers (`audioCache`) and the local timeline clone; scheduling
   AudioBufferSourceNodes with gain-automation fades reproduces the render
   math closely enough for editing decisions. Instant seek/scrub, works while
   unrendered, zero wire-protocol changes.

## Decision

Timeline preview is client-side WebAudio (option 2). It plays on the browser's
device, not through the show's Outputs or master trim — the same honest
"approximate preview on this device" contract as the library editor's
"This device" audition. Bit-exact verification remains available by
auditioning the rendered result from the library panel.

## Consequences

- Preview correctness must track the renderer's envelope math (gain sum,
  linear/equal-power fades) manually; divergence shows up as "preview sounded
  different from the show". Acceptable: fades/gains are perceptually simple.
- No backend/protocol surface was added; the editor works fully offline from
  the server's render pipeline.
- Remote clients preview on their own machine's output, which is what an
  operator editing from a tablet actually wants.
