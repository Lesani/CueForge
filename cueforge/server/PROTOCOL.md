# CueForge Server Protocol

The frontend is built against **this** document. It describes the WebSocket and
REST contract exactly as implemented in `cueforge/server/`.

- Server: FastAPI + uvicorn, default port **7070**, plain HTTP on the LAN.
- The server is authoritative. All actions are processed **serially** and every
  change results in a full state snapshot broadcast to all clients.
- Cue identity for the engine is the **placement id** (used as the engine
  `cue_id`). Re-firing a placement restarts that voice; two placements of one
  library item are distinct voices. Engine `cue_id`s map 1:1 back to placement
  ids in the snapshot.

---

## Authentication

- **Loopback is trusted**: requests/WS from `127.0.0.1` / `::1` never need a PIN.
- **Remote clients** must present the configured PIN. If no PIN is configured
  (empty string), the server is open.
  - REST: header `X-CueForge-Pin: <pin>` **or** query `?pin=<pin>`.
  - WebSocket: query `?pin=<pin>` on the `/ws` URL.
- Wrong/missing PIN from a remote client: REST returns **401**; the WebSocket is
  closed with code **1008**.

---

## WebSocket `/ws`

On connect the server immediately sends one full **state snapshot**, then
broadcasts an updated snapshot to every client after each processed action and on
the ~15 Hz status tick.

### Client -> server

```json
{ "action": "<name>", ...params }
```

Unknown actions or bad params yield an `{"type":"error","message":...}` frame to
that client only (they do not disconnect it).

| Action | Params | Effect |
|---|---|---|
| `go` | — | Fire the cue at the current page cursor, advance the cursor, start a 500 ms global GO lock. Ignored while the lock is active or when parked at the end of the page. |
| `fire` | `placementId` | Fire that placement (tap); set the page cursor to its sequence index + 1. Firing a background advances the cursor over it. |
| `standby` | `placementId` | Silently set the page cursor to that placement's index (no audio). |
| `cursorMove` | `direction` (`"up"`/`"down"`/`"left"`/`"right"`) | Silent cursor move. up/down = -1/+1 within the sequence (clamped 0..len). left/right = jump to the first cue of the previous/next column (clamped). |
| `setPage` | `pageId` | Switch the shared current page. |
| `setEditMode` | `on` (bool) | Toggle the shared edit mode. |
| `panic` | — | `engine.panic()` (fast ~150 ms fade of everything). Never debounced. |
| `reset` | — | Set every page cursor to 0 and `engine.panic()` (pre-show slate). |
| `placeCue` | `libraryItemId`, `page`, `column`, `row` | Create a placement at a grid cell. |
| `moveCue` | `placementId`, `toColumn`, `toRow` | Move a placement. If the target cell is occupied, **swap** the two placements' positions. |
| `removePlacement` | `placementId` | Remove a placement (library item stays). |
| `deleteLibraryItem` | `libraryItemId` | Remove the library item **and all its placements**. |
| `addColumn` | `page`, `name`, (opt) `rows` | Append a column (default 8 rows). |
| `renameColumn` | `columnId`, `name` | Rename a column. |
| `removeColumn` | `columnId` | Remove a column and its placements. |
| `setRows` | `columnId`, `rows` | Set a column's row count. |
| `addPage` | `name` | Append a page. |
| `renamePage` | `pageId`, `name` | Rename a page. |
| `removePage` | `pageId` | Remove a page and its placements. |
| `updateLibraryItem` | `libraryItemId`, `fields` | Patch item params (see below). |
| `duplicateLibraryItem` | `libraryItemId` | Clone the item (shares audio; name + `" (copy)"`). |
| `createStopCue` | (opt) `name` (default `"Stop all backgrounds"`), (opt) `page`+`column`+`row` | Create an audio-less control cue: `type "stop"`, `stopTarget "allBackgrounds"`, `stopMode "hard"`, `stopFadeSeconds 0`. When all three grid params are given, also places it at that cell in the same action. |
| `createFadeCue` | (opt) `name` (default `"Fade"`), (opt) `page`+`column`+`row` | Create an audio-less fade control cue: `type "fade"`, `fadeTarget "allBackgrounds"`, `fadeToDb 0`, `fadeTimeSeconds 3`, `fadeStopWhenDone false`. When all three grid params are given, also places it at that cell in the same action. |
| `normalizeItem` | `libraryItemId` | Peak-normalize to ~-1 dBFS (sets `gainDb`). |
| `auditionItem` | `libraryItemId` | Play the item on the engine's audition channel (server out). |
| `stopAudition` | — | Stop the audition channel. |
| `updatePlacement` | `placementId`, `fields` (`triggerMode`, `preWait`, `outputId`) | Patch a placement's sequencing and routing. The server validates `triggerMode` in `{onTrigger, withPrevious, afterPrevious}` and `preWait >= 0`, and **rejects** `afterPrevious` when the predecessor in the page sequence loops (its End is undefined). `outputId` is an Output override (`null`/falsy inherits the item). Invalid values leave the field unchanged. Autosaves. |
| `pause` | — | Freeze all currently-playing show voices (~10 ms declick) **and** freeze pending scheduled fires. Idempotent. Audition is unaffected. A cue fired while paused plays normally. |
| `resume` | — | Un-freeze paused voices (~10 ms declick) and resume scheduled countdowns. Idempotent. |
| `setOutputs` | `outputs` (full list) | Replace the show's Outputs. Each: `{id, name, device (device NAME string \| null), channel (1-based first channel, >=1), mono (bool)}`. Server validates (name non-empty, channel >= 1, unique ids), persists to `show.settings.outputs`, reconfigures engines, autosaves. |
| `testOutput` | `outputId` | Play a generated identification tone on that Output via the audition channel (stereo: L then R beep; mono: single beep). |
| `createCompound` | (opt) `name` (default `"Compound"`), (opt) `page`+`column`+`row` | Create a compound cue: `type "compound"`, empty `timeline {"tracks":[]}`, `renderState "pending"`. When all three grid params are given, also places it at that cell. Carries no audio until rendered. |
| `updateTimeline` | `itemId`, `timeline` | Replace the compound's timeline (server-sanitized), set `renderState "pending"`, and schedule a debounced background render. A fundamentally malformed `timeline` (not an object, or `tracks` not a list) yields an error frame. |
| `renderCompound` | `itemId` | Force an immediate (undebounced) re-render of the compound. |

`updateLibraryItem.fields` accepts these keys (camelCase wire form; snake_case
model names are also accepted): `name`, `background`, `trimIn`, `trimOut`, `gainDb`,
`fadeIn`, `fadeOut`, `fadeShape`, `loop`, `outputId`, `group`, `stopTarget`,
`stopMode`, `stopFadeSeconds`, `fadeTarget`, `fadeToDb`, `fadeTimeSeconds`,
`fadeStopWhenDone`.
Unknown keys are ignored, **including `type`** -- the meta type is immutable after
creation (ADR 0006), so a client patch that carries `type` is silently dropped.
`background` (bool) is coerced and accepted only for `normal`/`compound` items;
for `stop`/`fade` items it is ignored (they are always non-background).
(`fadeShape` doubles as the fade-cue ramp shape.)
Changing `gainDb` also ramps any running voice of the item to the new gain over
~50 ms (a live edit; a no-op for placements that are not currently playing);
`normalizeItem` applies the same live ramp.

`outputId` (string | null, default null) is a library item's default Output id --
one of the Outputs defined in `show.settings.outputs`. `null`/absent means the
Default Output (channels 1-2 on the configured output device). A Placement's
`outputId` overrides the item's. Dangling ids (an Output that was deleted) fall
back to the Default Output. Only meaningful for audio-producing cues (`normal`/
`compound`, whether or not they are backgrounds). The older `outputPair` field has
been removed and is silently ignored on load.

The defined Outputs live in `show.settings.outputs` (part of the portable show):
a list of `{id, name, device (device NAME string | null), channel (1-based first
channel, >=1), mono (bool)}`, edited via `setOutputs`. Each is a named routing
destination on a specific device + channel; `mono` picks a single channel
(mean-downmix) rather than a stereo pair. The Default Output is implicit and not
stored in this list.

`group` is an optional flat, UI-only grouping label on a library item (default
`""`, meaning ungrouped). It persists in `show.json` and appears in every library
item dict; the server does not build any group hierarchy -- the UI groups by it.
Shows saved before the rename carry `folder` instead; those load into `group`.

`type` (the **meta type**) is one of `normal` (imported sound), `compound`,
`stop`, or `fade`, fixed at creation and never convertible (ADR 0006).
`background` (bool, default `false`) is the item's **role**: `true` = a
stackable/loopable background layer, `false` = an exclusive normal channel. It is
meaningful only for `normal` and `compound` items; the model and reducer force it
`false` for `stop`/`fade`. Loop is honored only when `background` is `true`.
Legacy migration: shows saved with the old `type "background"` load as
`type "normal"` + `background true` (tolerant read, no `formatVersion` bump, same
policy as the removed `outputPair`); saves always write the new shape.

#### Compound cues

A **compound** library item (`type "compound"`) is assembled from other library
sounds on an internal multi-track `timeline`, flattened offline into a single
audio blob stored as its ordinary `audio_hash` (see ADR 0004). Once rendered it
fires, auditions, exports, trims, fades and loops exactly like an imported audio
item -- there are **no compound-specific firing branches**. A show always fires
the **last completed** render, even while newer timeline edits are still pending.

Every library item dict in the state snapshot now carries four additional
server-managed fields (present on all items; only meaningful for compounds):

- `timeline` (object | null): `{"tracks":[{id,name,gainDb,mute,clips:[...]}]}`.
  Each clip: `{id, itemId (source library item id), start, clipIn, clipOut
  (null = source end), gainDb, fadeIn, fadeOut, fadeShape ("linear"|"equalPower"),
  effects: []}`. Times are seconds. A clip whose `itemId` no longer resolves
  renders as silence.
- `renderState` (string): `""` | `"pending"` | `"rendering"` | `"ready"` | `"error"`.
- `renderError` (string): human-readable render failure (truncated), else `""`.
- `renderSignature` (string): hash of the timeline + each source's audio hash at
  the last render; a mismatch on project open triggers a background re-render.

These four fields are **server-managed** and are NOT patchable via
`updateLibraryItem` -- the timeline is edited only through `updateTimeline`.
Firing a compound that has never rendered (`audioHash` null) yields an error
frame (no audio), the same refusal as any audio-less item. Deleting a source
library item that a compound references schedules that compound to re-render
(the missing clip becomes silence) rather than waiting for the next open.

All mutating edit/library actions autosave `show.json` to the working folder.

### Placement sequencing (`triggerMode` / `preWait`)

Every placement carries two sequencing fields (defaults keep old shows working):

- `triggerMode` — how the placement starts: `onTrigger` (a human GO — default),
  `withPrevious` (starts when the previous placement in sequence fires), or
  `afterPrevious` (starts when the previous placement in sequence ends).
- `preWait` — seconds (>= 0, default 0) between being triggered (by GO or by a
  chain) and audio actually starting.

A **Chain** is a maximal run of consecutive placements in the column-major page
sequence whose `triggerMode` is `withPrevious`/`afterPrevious`. One GO on the
placement before them plays the whole chain; a chain never crosses a page
boundary (a `withPrevious`/`afterPrevious` placement that is first in its page
sequence is treated as a head). Chains are resolved into frame-offset schedules
at GO time and counted down on the engine clock (see the Chains firing rules).

### Firing rules (placement -> its library item)

A `normal` or `compound` item routes by its **`background` role flag** (ADR 0006),
not its type:
- **background == false**: `play_normal(placementId, pcm, gain_db, fade_in, fade_out, fade_shape, output_id)`.
- **background == true**: `play_background(placementId, pcm, gain_db, fade_in, loop, fade_shape, output_id)`
  (a compound background fires its rendered blob on the background bus; loop loops
  the blob).
- **stop**: no audio.
  - `stopTarget == "allBackgrounds"`: `stop_all_backgrounds(mode=stopMode, fade_seconds=stopFadeSeconds)`.
  - otherwise (a specific library item id): the server reads the engine's running
    backgrounds, maps each running `cue_id` back to its placement, and calls
    `stop_background(placementId, mode=stopMode, fade_seconds=stopFadeSeconds)` for
    **every** running background placement whose `libraryItemId` equals
    `stopTarget`. (Because the engine is keyed by placement id, a "stop this
    background" cue stops all currently-running placements of that library item.)
- **fade**: no audio. Ramps the *live gain* of running voices to `fadeToDb` over
  `fadeTimeSeconds` using `fadeShape` (`linear`/`equalPower`).
  - `fadeTarget == "allBackgrounds"`: `set_all_backgrounds_gain(fadeToDb, fadeTimeSeconds, ...)`
    ramps every running background.
  - otherwise (a specific library item id): ramps **every** running voice — normal
    OR background — whose placement's `libraryItemId` equals `fadeTarget`
    (`set_cue_gain(placementId, ...)`).
  - `fadeStopWhenDone true` drops each ramped voice (declicked, click-free for any
    target level) when its ramp completes. A fading background keeps showing in
    status and can be re-fired mid-fade (unlike a stop, which removes it at once).

### Chains (resolution at GO)

When a GO (or `fire`) lands on a chain head, the server resolves the **whole**
chain from the show model — no PCM needed for the math — into per-member frame
offsets and hands the engine a countdown for each:

- **head**: starts at `preWait_head` frames.
- **withPrevious** member: starts at the predecessor's *fire time* + its own
  `preWait`.
- **afterPrevious** member: starts at the predecessor's *End* + its own
  `preWait`. If the predecessor loops (End undefined), the chain **breaks** at
  that member: it and the rest fall out and become manual cues. A **stop** cue's
  End is its fade time (0 when hard); a **fade** cue's End is `fadeTimeSeconds`.
- A member whose offset is 0 fires live (identical to a tap); a member with a
  positive offset is armed and counted down sample-accurately by the engine.

The page cursor parks one past the last chained member. Re-firing a chain cancels
its own pending members and reschedules them; chains launched by other GOs keep
running. PANIC, `reset`, and deleting a placement cancel its pending fires. A
chained specific-target stop **or fade** catches voices running at GO time
**plus** those started earlier in the same chain; an `allBackgrounds` fade
resolves the live backgrounds at activation time, not at GO. Edits to
`triggerMode`/`preWait` do not re-time a chain that is already running.

### Server -> client: state snapshot

```json
{
  "type": "state",
  "show": <Show.to_dict() | null>,
  "runtime": {
    "currentPage": "<pageId>" | null,
    "editMode": false,
    "sequence": ["<placementId>", ...],
    "cursorIndex": 0,
    "playing": null | {"placementId": "..", "cueId": "..", "frame": 0, "totalFrames": 0},
    "backgrounds": [
      {"placementId": "..", "cueId": "..", "frame": 0, "totalFrames": 0, "loop": true}
    ],
    "auditionActive": false,
    "audition": null | {"libraryItemId": "..", "frame": 0, "totalFrames": 0},
    "paused": false,
    "scheduled": [
      {"placementId": "..", "cueId": "..", "kind": "normal", "remainingMs": 0}
    ],
    "goLockRemainingMs": 0,
    "deviceOk": true,
    "deviceChannels": 2,
    "outputs": [{"id": "..", "deviceOk": true, "deviceChannels": 2}],
    "loading": null | {"done": 0, "total": 0},
    "clients": 1
  }
}
```

- `deviceChannels` is the current (default) output device's channel count (2 when
  no device is open or headless). It still drives the default Output's channel
  math in the UI.
- `outputs` is per-Output availability for the UI marks: one `{id, deviceOk,
  deviceChannels}` per defined Output (from `show.settings.outputs`). `deviceOk`
  is false while that Output's device is missing (its cues fall back to the
  Default Output meanwhile); `deviceChannels` is that device's channel count. The
  Default Output is implicit and not listed here. Additive/backward-compatible
  (a FakeEngine or older caller yields `[]`).

- `paused` is true while the engine is globally frozen (see the `pause`/`resume`
  actions). `scheduled` lists pending chain fires ("armed" cells): each carries
  the firing placement's id as `placementId`/`cueId` (for a stop cue, the stop
  placement itself, not its target), its `kind`
  (`normal`/`background`/`stop`/`fade`), and `remainingMs` until it fires (frozen
  while `paused`). Both fields are additive and backward-compatible.

- `sequence` is the GO order for `currentPage`: **column-major** (each column
  top-to-bottom, columns left-to-right), empty cells skipped.
- `cursorIndex` is `0..len(sequence)` (`len` = parked at the end).
- Client derivation:
  - **played / green** = `sequence[0 : cursorIndex]`, excluding the one currently
    in `playing`.
  - **standby cell** = `sequence[cursorIndex]` (hidden while `playing` is non-null;
    the cursor vanishes during a normal cue).

### Server -> client: error

```json
{ "type": "error", "message": "<text>" }
```

---

## REST

All REST endpoints require auth (loopback trusted; otherwise PIN).

| Method + path | Body | Returns |
|---|---|---|
| `GET /` | — | `web/index.html` (or a JSON status stub if the UI isn't built yet). |
| `GET /static/*` | — | Static assets mounted from `cueforge/web/`. |
| `POST /api/import` | multipart `file` (or, if `python-multipart` is unavailable, **raw body** with `?filename=`) | `{"status":"new"\|"duplicate","audioHash":..,"item":<dict\|null>,"matches":[<dict>...]}`. On `"new"`, a snapshot is broadcast. |
| `POST /api/import/clone` | `{"audioHash":..,"name":..}` | The new library item dict; broadcasts a snapshot. |
| `GET /api/audio/{hash}` | opt query `?format=wav\|flac` | The stored audio for waveform + this-device audition. Default / `flac` -> the stored FLAC bytes (`audio/flac`). `wav` -> transcoded 16-bit PCM WAV, 48 kHz stereo (`audio/wav`) for iOS Safari, which can't decode FLAC. Any other value -> 400; missing hash -> 404. |
| `GET /api/export/{libraryItemId}` | query `?format=mp3\|wav\|flac` | Transcode the item's stored audio with its cue params applied and return a browser download. Applies `trimIn`/`trimOut`, `gainDb`, `fadeIn`/`fadeOut` (fade-out at trimmed-duration - `fadeOut`); loop is NOT applied. `wav` = 16-bit PCM, `flac` = FLAC, `mp3` = libmp3lame 320 kbps. `Content-Disposition: attachment` with an ASCII filename + RFC 5987 `filename*` (UTF-8) for umlauts. 404 unknown item / missing audio; 400 for stop-type items (no audio) or a bad format. |
| `GET /api/devices` | — | `AudioEngine.list_output_devices()` (`[]` on failure). Each device dict carries `max_output_channels` (used by the client to offer output pairs). |
| `GET /api/settings` | — | The full config dict: `{"outputDevice","masterDb","pin","port","theme","checkForUpdates"}` plus bookkeeping keys once set (`lastProject`, `ffmpegDismissedVersion`). |
| `POST /api/settings` | any subset of the settings keys | The full merged settings; persisted to `~/CueForge/config.json`. Setting `outputDevice` (re)starts engine output. Setting `masterDb` applies it as the engine master trim (a device-level gain over the summed mix, ~50 ms smoothing; affects show + audition; also applied on startup). Broadcasts a snapshot. |
| `GET /api/connection` | — | `{"url","lanUrl","pin","qr"}` where `qr` is a PNG **data URL** of the LAN URL with the PIN embedded as `?pin=`. |
| `POST /api/project/new` | `{"name":..}` | `{"name":..}`; opens a fresh project; broadcasts. |
| `POST /api/project/open` | `{"name":..}` | `{"name":..}`; opens `~/CueForge/projects/<name>.cueforge`; broadcasts. |
| `POST /api/project/save` | — | `{"name":..,"path":..}`; repackages the working folder into the `.cueforge`. |
| `POST /api/project/rename` | `{"name": ".."}` | `{"name":..}`; renames the open project (and its saved `.cueforge` if present). |
| `GET /api/projects` | — | `[{"name":..,"path":..}, ...]` recent projects. |
| `GET /api/project/download` | — | The current project packaged as a portable `.cueforge` (`application/octet-stream`), `Content-Disposition: attachment` named `<ProjectName>.cueforge` (ASCII + RFC 5987 `filename*`). For carrying a show to another machine (USB). 409 if no project is open. |
| `POST /api/project/upload` | multipart `file` (a `.cueforge`; or raw body with `?filename=`) | `{"name":..}`. Validates by extracting, stores it as a saved project **auto-renamed to `Name (2)` etc. on name collision** (never overwrites), opens it, and broadcasts. 400 if the file is not a valid `.cueforge`. |
| `GET /api/update/status` | — | App self-update status: `{"current","latest","url","updateAvailable","canApply","checkEnabled","checked","phase","percent","downloaded","total","error"}`. `latest` comes from the periodic GitHub Releases check (gated on the `checkForUpdates` setting); `canApply` is true only for the packaged exe; `phase` is `idle\|downloading\|restarting\|error`. Never blocks on the network. |
| `POST /api/update/check` | — | Forces an immediate GitHub check (even when the periodic check is disabled) and returns the same status payload. |
| `POST /api/update/apply` | — | Downloads the release `CueForge.exe`, verifies it against `SHA256SUMS.txt`, rename-swaps the running exe and gracefully restarts the server (the launcher spawns the new build). Returns `{"started":bool, ...status}`. 409 when running from source or no update is available. |

### Config / storage locations (per machine, not in the portable show)

- `~/CueForge/config.json` — server settings (output device, master dB, PIN, port, theme).
- `~/CueForge/projects/<name>.cueforge` — saved portable shows.
- `~/CueForge/work/<name>/` — extracted working folder (continuous autosave target).

A `.cueforge` file is a zip of `show.json` + `audio/<sha256>.flac`. `show.json`
carries a top-level `formatVersion` (currently `1`; absent = `1`): older files
are migrated on load, files from a **newer** format are refused with a clear
error instead of being misread.

### Status broadcast loop

A background task runs at ~15 Hz: it reads `engine.get_status()`, maps engine
`cue_id`s to placement ids to fill `runtime.playing` / `runtime.backgrounds`
(with `frame` / `totalFrames`), updates `deviceOk`, and broadcasts the snapshot.
On startup the engine output is started best-effort with the configured device;
if it fails, `deviceOk` stays `false` and the UI warns (the server never crashes
and never falls back to a different device).
