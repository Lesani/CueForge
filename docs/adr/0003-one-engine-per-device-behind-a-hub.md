# 3. One AudioEngine per output device, behind a routing hub

Date: 2026-07-06

## Status

Accepted

## Context

Named Outputs let cues play on *different sound cards* (main PA, a surround
send, a practical speaker behind the audience), including splitting a stereo
device into two mono channels. Today one `AudioEngine` owns one sounddevice
`OutputStream`; all routing happens by scattering stereo voices into that
single device's N-channel bus.

Independent sound cards have independent hardware clocks. Options considered:

1. **One engine per device behind a hub** — each device keeps its own
   `AudioEngine` (mixer + stream + reconnect loop), a thin `EngineHub`
   routes control calls by the cue's Output and fans global operations out.
2. **One master mixer feeding per-device ring buffers** — a single render
   clock, each device callback pulls from its buffer. Requires adaptive
   resampling per device to absorb clock drift, priming/latency tuning, and
   entangles every device's failure modes with the shared clock.
3. **Stay single-device** and require an aggregate device (ASIO4ALL etc.) —
   pushes clock drift onto third-party drivers, poor out-of-box experience.

## Decision

Option 1. `AudioEngine` stays a self-contained single-device mixer;
a new `EngineHub` owns one engine per distinct device referenced by the
show's Outputs (plus the default device) and exposes the controller-facing
API: it resolves `output_id` → (engine, first channel, mono) at fire/schedule
time, fans out pause/resume/panic/master-gain/cancel-all, and merges status.

Cross-device **normal exclusivity** is enforced by the hub: it tracks which
engine holds the live normal voice, declick-kills it on a different engine
before a live fire, and for scheduled (chain) normal fires schedules a
`stop_normal` at the same frame offset on every other engine.

## Consequences

- Sample-accurate timing (P1 chains, scheduled fires) holds *within* one
  device, exactly as before. *Across* devices, timing is aligned only to
  within normal inter-device clock skew/callback jitter (milliseconds) —
  accepted: chains are show sequencing, not phase-aligned layering. Do not
  build features that assume sample sync across two Outputs on different
  cards.
- Per-device failure isolation for free: one card unplugged → that engine's
  reconnect loop spins, all other Outputs keep playing.
- Every global operation added to `AudioEngine` later MUST be routed through
  the hub (fan-out or targeted) — the controller must never hold a direct
  engine reference again.
- A paused show freezes every engine's countdowns; the hub is the single
  place that knows the global paused flag.
- Outputs on the *same* device share one engine and stay sample-synced.
