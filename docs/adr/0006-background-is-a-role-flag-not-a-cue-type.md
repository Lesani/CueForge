# 6. Background is a role flag, not a cue type

Date: 2026-07-07

## Status

Accepted

## Context

`LibraryItem.type` conflated two independent ideas: what a cue *is*
(imported sound, timeline compound, stop control, fade control) and how it
*plays* (exclusive "normal" channel vs. stackable background layer).
"background" sat in the same enum as "stop" and "compound", which produced
absurd edges: the UI let a stop cue be converted into a background cue, and
a compound cue could not be an ambience loop at all because its type slot
was already taken by "compound".

## Decision

- `type` is the **meta type** only: `normal` (sound), `compound`, `stop`,
  `fade` — fixed at creation, never convertible.
- A new boolean **`background`** on LibraryItem is the **role**, meaningful
  only for `normal` and `compound`: False = exclusive normal channel,
  True = stackable/loopable background layer. Stop/Fade force it False.
- Engine routing, loop eligibility, fade/stop targeting and chain-end
  semantics key off the role flag, not the type.
- Legacy shows with `type == "background"` are migrated on load to
  `type="normal", background=True` (tolerant read, same policy as the
  removed `outputPair`).

## Consequences

- Compound cues can now be backgrounds (looping ambience built in the
  timeline editor) with zero extra engine work — the role branch already
  plays the rendered blob.
- The library UI stops offering type conversion; it offers a role toggle on
  sound/compound cues only. Fewer states, no nonsense transitions.
- Old builds reading a NEW show would see backgrounds as plain normals
  (unknown `background` key ignored). Accepted: pre-1.0, single dev stream.
