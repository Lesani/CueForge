# Chain timing runs on the engine's audio clock, not controller timers

Cue chains (`withPrevious` / `afterPrevious` trigger modes with per-placement
pre-waits) need something to fire followers at the right moment after one GO.
We schedule them inside the audio engine: at GO time the controller resolves
the whole chain — loads each follower's PCM and computes each start as a frame
offset — and hands the engine voices with a `start_in_frames` countdown that
the render loop counts down sample-accurately. We rejected controller-side
asyncio/threading timers because they run in a second time domain: wait timing
would jitter with the OS scheduler, pause would need cancel-and-remember-remaining
bookkeeping, and wall-clock timers would break the engine's stated principle
that the offline-rendered test suite hears exactly what the booth hears.

## Consequences

- Pause is correct by construction: freezing the engine clock freezes audio
  and pending waits as one thing. PANIC clears scheduled voices in the same
  command that kills live ones.
- Chains are resolved at GO time: edits mid-flight don't affect a running
  chain, and an `afterPrevious` follower fires at its precomputed time even if
  its predecessor is cut early. Operator intervention (PANIC/reset/stop)
  cancels pending scheduled fires.
- Firing a chain head loads all members' FLACs synchronously (there is no PCM
  cache). Accepted for now — chains are typically short SFX bursts; add
  standby pre-loading if it proves audibly slow.
