# Voices stay stereo sources routed to an N-channel bus via output pairs

Multichannel output widens the engine's mix bus from hard-coded stereo to the
device's native channel count (capped at 32), but voices remain stereo
sources: each Library Item selects an output pair (Out 1-2, 3-4, ..., default
1-2) that its stereo signal is summed into. We rejected a full per-cue 2xN
crosspoint matrix (as in professional cue software) for v1 — output-pair routing covers the real
use cases (music to mains, SFX to surrounds, click to monitors) with one
dropdown, and a matrix can be layered on later without re-architecting,
whereas starting with a matrix forces UI and model complexity onto every cue
now. Multi-device output stays out of scope. Storage is unaffected (stereo
FLAC); routing lives on the Library Item (part of "how it plays", like gain
and fades — deliberately unlike Trigger Mode, which is positional and lives
on the Placement). Sends to channels the current device lacks are dropped
with a UI warning; a mono device still gets the averaged full-mix downmix.
