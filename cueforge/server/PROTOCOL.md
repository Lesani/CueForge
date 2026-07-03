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
| `normalizeItem` | `libraryItemId` | Peak-normalize to ~-1 dBFS (sets `gainDb`). |
| `auditionItem` | `libraryItemId` | Play the item on the engine's audition channel (server out). |
| `stopAudition` | — | Stop the audition channel. |

`updateLibraryItem.fields` accepts these keys (camelCase wire form; snake_case
model names are also accepted): `name`, `type`, `trimIn`, `trimOut`, `gainDb`,
`fadeIn`, `fadeOut`, `fadeShape`, `loop`, `group`, `stopTarget`, `stopMode`,
`stopFadeSeconds`. Unknown keys are ignored.

`group` is an optional flat, UI-only grouping label on a library item (default
`""`, meaning ungrouped). It persists in `show.json` and appears in every library
item dict; the server does not build any group hierarchy -- the UI groups by it.
Shows saved before the rename carry `folder` instead; those load into `group`.

All mutating edit/library actions autosave `show.json` to the working folder.

### Firing rules (placement -> its library item)

- **normal**: `play_normal(placementId, pcm, gain_db, fade_in, fade_out, fade_shape)`.
- **background**: `play_background(placementId, pcm, gain_db, fade_in, loop, fade_shape)`.
- **stop**: no audio.
  - `stopTarget == "allBackgrounds"`: `stop_all_backgrounds(mode=stopMode, fade_seconds=stopFadeSeconds)`.
  - otherwise (a specific library item id): the server reads the engine's running
    backgrounds, maps each running `cue_id` back to its placement, and calls
    `stop_background(placementId, mode=stopMode, fade_seconds=stopFadeSeconds)` for
    **every** running background placement whose `libraryItemId` equals
    `stopTarget`. (Because the engine is keyed by placement id, a "stop this
    background" cue stops all currently-running placements of that library item.)

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
    "goLockRemainingMs": 0,
    "deviceOk": true,
    "loading": null | {"done": 0, "total": 0},
    "clients": 1
  }
}
```

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
| `GET /api/devices` | — | `AudioEngine.list_output_devices()` (`[]` on failure). |
| `GET /api/settings` | — | The full config dict: `{"outputDevice","masterDb","pin","port","theme","checkForUpdates"}` plus bookkeeping keys once set (`lastProject`, `ffmpegDismissedVersion`). |
| `POST /api/settings` | any subset of the settings keys | The full merged settings; persisted to `~/CueForge/config.json`. Setting `outputDevice` (re)starts engine output. Broadcasts a snapshot. |
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
