# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Lesani. See LICENSE for details.
"""FastAPI application: static UI, REST, WebSocket, and the status broadcast loop.

The app owns a single :class:`~cueforge.server.controller.ShowController` and a
real :class:`~cueforge.engine.AudioEngine`. All WebSocket actions are processed
SERIALLY under an asyncio lock; after every action the new snapshot is broadcast
to all clients. A ~15 Hz background task reads engine status and rebroadcasts so
playing/background progress stays live.

Auth: loopback clients are always trusted; remote clients must
present the configured PIN (``?pin=`` on the WS URL, or ``X-CueForge-Pin`` header
/ ``?pin=`` on REST). Wrong/missing PIN from a remote client -> 401 / WS close.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import shutil
import tempfile
import zipfile
from pathlib import Path

from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

# Multipart uploads require the optional ``python-multipart`` dependency. When it
# is absent we fall back to a raw-body upload route so the app still imports/runs.
try:  # pragma: no cover - environment dependent
    import multipart as _multipart  # noqa: F401

    _HAS_MULTIPART = True
except Exception:  # pragma: no cover
    _HAS_MULTIPART = False

from cueforge import ffmpeg_util, update_util
from cueforge.engine import AudioEngine, EngineHub
from cueforge.project import (
    ProjectSession,
    add_clone,
    import_audio,
    make_column,
    make_page,
)
from cueforge.project import youtube
from cueforge.project.exporter import (
    EXPORT_FORMATS,
    ExportError,
    content_disposition,
    export_cue,
    media_type_for,
    transcode_to_wav,
)
from cueforge.project.importer import ImportError as AudioImportError
from cueforge.project.youtube import YouTubeError
from cueforge.server import connection, protocol
from cueforge.server.controller import ShowController

# --------------------------------------------------------------------------
# Paths / config
# --------------------------------------------------------------------------
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
HOME_DIR = Path(os.path.expanduser("~")) / "CueForge"
PROJECTS_DIR = HOME_DIR / "projects"
WORK_ROOT = HOME_DIR / "work"
CONFIG_PATH = HOME_DIR / "config.json"

DEFAULT_CONFIG = {
    "outputDevice": None,
    "masterDb": 0.0,
    "pin": "",
    "port": 7070,
    "theme": "graphite",
    "checkForUpdates": True,
}

STATUS_HZ = 15
BROADCAST_INTERVAL = 1.0 / STATUS_HZ

# Windows reserved device names: a project must never be named one of these, or
# saving ``<name>.cueforge`` fails. (Case-insensitive, extension irrelevant.)
_WIN_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

# Characters that cannot appear in a Windows filename (plus control chars).
# Crucially this includes the path separators, so a validated project name can
# never escape PROJECTS_DIR (``..\..\evil`` etc.).
_INVALID_NAME_CHARS = set('<>:"/\\|?*') | {chr(i) for i in range(32)}


def validate_project_name(name: str) -> str:
    """Return the stripped project name, or raise 400 if it cannot be used as
    a filename stem inside PROJECTS_DIR (path separators, traversal, reserved
    device names, trailing dots)."""
    name = (name or "").strip()
    if (
        not name
        or name != name.rstrip(".")
        or any(c in _INVALID_NAME_CHARS for c in name)
    ):
        raise HTTPException(status_code=400, detail="invalid project name")
    if name.split(".")[0].upper() in _WIN_RESERVED:
        raise HTTPException(
            status_code=400, detail="invalid project name (reserved on Windows)"
        )
    return name


def _safe_remove(path: str) -> None:
    """Delete a temp file, ignoring errors (used as a response cleanup task)."""
    try:
        os.remove(path)
    except OSError:
        pass


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            cfg.update(json.load(fh))
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return cfg


def save_config(cfg: dict) -> None:
    HOME_DIR.mkdir(parents=True, exist_ok=True)
    tmp = str(CONFIG_PATH) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
    os.replace(tmp, CONFIG_PATH)


# --------------------------------------------------------------------------
# Connection manager (WebSocket fan-out + serial dispatch)
# --------------------------------------------------------------------------
class ConnectionManager:
    def __init__(self) -> None:
        self.active: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    @property
    def count(self) -> int:
        return len(self.active)

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.active.add(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self.active.discard(ws)

    async def broadcast(self, message: dict) -> None:
        dead = []
        for ws in list(self.active):
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    async def close_all(self, code: int = 1012) -> None:
        """Close every client connection (1012 = service restart), so a
        graceful server shutdown isn't blocked waiting for open sockets."""
        for ws in list(self.active):
            try:
                await ws.close(code=code)
            except Exception:
                pass
            self.disconnect(ws)


# --------------------------------------------------------------------------
# App state container
# --------------------------------------------------------------------------
class AppState:
    def __init__(self) -> None:
        self.config = load_config()
        self.engine = EngineHub()
        self.controller = ShowController(self.engine)
        from cueforge.server.compound_render import CompoundRenderManager
        self.render_manager = CompoundRenderManager(self)
        self.controller.on_compound_dirty = self.render_manager.schedule
        self.manager = ConnectionManager()
        self.dispatch_lock = asyncio.Lock()
        self._status_task: asyncio.Task | None = None
        # Set by the launcher (uvicorn.Server); lets the self-update endpoint
        # request a graceful stop of the serving loop.
        self.uvicorn_server = None
        # The serving event loop, captured at startup so worker threads (the
        # update installer) can schedule coroutines onto it.
        self.loop: asyncio.AbstractEventLoop | None = None

    def snapshot(self) -> dict:
        return self.controller.build_snapshot(clients=self.manager.count)

    async def apply_and_broadcast(self, action: str, params: dict) -> None:
        """Serialize a reducer action then broadcast the fresh snapshot."""
        async with self.dispatch_lock:
            self.controller.dispatch(action, params)
        await self.manager.broadcast(self.snapshot())

    async def broadcast_state(self) -> None:
        await self.manager.broadcast(self.snapshot())


async def youtube_import_events(session, url, broadcast=None):
    """Yield NDJSON-shaped progress dicts for a YouTube import.

    Downloads bestaudio via yt-dlp into a temp dir, then funnels the file
    through the same :func:`import_audio` pipeline as ``/api/import``. Yields
    ``{"phase": ...}`` dicts (``updating`` / ``downloading`` / ``importing`` /
    ``done`` / ``error``). ``broadcast`` is an async callback invoked once, after
    a *new* item is added, so all clients refresh. Kept at module scope (not a
    route closure) so it is directly unit-testable without an HTTP client.
    """
    tmp_dir = tempfile.mkdtemp(prefix="cf_yt_")
    try:
        yield {"phase": "updating"}
        await youtube.ensure_updated()

        yield {"phase": "downloading", "percent": 0}
        path = None
        title = None
        async for ev in youtube.download_audio(url, tmp_dir):
            if ev["type"] == "progress":
                yield {"phase": "downloading", "percent": ev["percent"]}
            elif ev["type"] == "done":
                path, title = ev["path"], ev["title"]

        if session is None:
            yield {"phase": "error", "detail": "no project open"}
            return

        yield {"phase": "importing"}
        result = await asyncio.to_thread(
            import_audio, session, path, name=title or None
        )

        payload = {
            "phase": "done",
            "status": result.status,
            "audioHash": result.audio_hash,
            "item": result.item.to_dict() if result.item else None,
            "matches": [m.to_dict() for m in result.matches],
        }
        if result.status == "new" and broadcast is not None:
            await broadcast()
        yield payload
    except (YouTubeError, AudioImportError) as exc:
        yield {"phase": "error", "detail": str(exc)}
    except Exception as exc:  # pragma: no cover - unexpected
        yield {"phase": "error", "detail": str(exc)}
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def create_app() -> FastAPI:
    app = FastAPI(title="CueForge")
    state = AppState()
    app.state.cf = state

    # ---- lifecycle -------------------------------------------------------
    @app.on_event("startup")
    async def _on_startup() -> None:
        # Best-effort audio output start; never crash if the device is missing.
        try:
            state.engine.set_default_device(state.config.get("outputDevice"))
        except Exception:
            pass
        try:
            state.engine.set_master_gain(float(state.config.get("masterDb", 0.0) or 0.0))
        except Exception:
            pass
        # Reopen the last project the operator was working in; if there is no
        # recorded project (or it no longer exists / fails to open), start with a
        # fresh empty project so there is always a usable blank canvas.
        if state.controller.session is None:
            session = None
            last = state.config.get("lastProject")
            if last:
                try:
                    path = _project_path(last)
                    if os.path.isfile(path):
                        session = ProjectSession.open(path, _work_dir_for(last))
                except Exception:
                    session = None
            if session is None:
                try:
                    session = ProjectSession.create_new(
                        str(WORK_ROOT / "Untitled"), "Untitled"
                    )
                    session.show.pages.append(
                        make_page("Page 1", [make_column("Column 1", 8)])
                    )
                    session.autosave()
                except Exception:
                    session = None
            if session is not None:
                state.controller.set_session(session)
        state.loop = asyncio.get_running_loop()
        # Re-render any compound whose signature is stale versus its stored blob
        # (an edited/re-imported source, or a render that never completed).
        try:
            state.render_manager.schedule_dirty_all()
        except Exception:
            pass
        state._status_task = asyncio.create_task(_status_loop())

    @app.on_event("shutdown")
    async def _on_shutdown() -> None:
        if state._status_task is not None:
            state._status_task.cancel()
        try:
            state.engine.stop_all_output()
        except Exception:
            pass

    async def _status_loop() -> None:
        while True:
            await asyncio.sleep(BROADCAST_INTERVAL)
            try:
                await state.broadcast_state()
            except Exception:
                pass

    # ---- auth helpers ----------------------------------------------------
    def _client_host(request: Request) -> str | None:
        return request.client.host if request.client else None

    def require_auth(request: Request) -> None:
        host = _client_host(request)
        if connection.is_local_address(host):
            return
        pin = state.config.get("pin") or ""
        if not pin:
            return  # no PIN configured -> open
        provided = request.headers.get("X-CueForge-Pin") or request.query_params.get(
            "pin"
        )
        if not secrets.compare_digest(str(provided or ""), str(pin)):
            raise HTTPException(status_code=401, detail="invalid or missing PIN")

    # ---- static UI -------------------------------------------------------
    @app.middleware("http")
    async def no_cache_ui(request: Request, call_next):
        # The UI is served locally and updated with the app; always revalidate so
        # a new version is never masked by a stale browser cache.
        response = await call_next(request)
        path = request.url.path
        if path == "/" or path.startswith("/static"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/")
    async def index() -> Response:
        index_file = WEB_DIR / "index.html"
        if index_file.is_file():
            return FileResponse(str(index_file))
        return JSONResponse(
            {"status": "CueForge server running", "web": "no index.html yet"}
        )

    if WEB_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

    # ---- WebSocket -------------------------------------------------------
    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        host = ws.client.host if ws.client else None
        if not connection.is_local_address(host):
            pin = state.config.get("pin") or ""
            if pin and not secrets.compare_digest(
                str(ws.query_params.get("pin") or ""), str(pin)
            ):
                # Accept the handshake first, THEN close with 1008. Closing
                # before accept makes uvicorn deny the handshake with an HTTP
                # 403, which the browser only sees as a generic 1006 failure --
                # the client can't tell it was an auth rejection and would show
                # the loud "connection lost" overlay instead of the PIN prompt.
                await ws.accept()
                await ws.close(code=1008)
                return
        await state.manager.connect(ws)
        try:
            # Send a full snapshot immediately on connect.
            await ws.send_json(state.snapshot())
            # Update client count for everyone.
            await state.broadcast_state()
            while True:
                raw = await ws.receive_text()
                try:
                    msg = json.loads(raw)
                    action = msg.get("action")
                    if action not in protocol.ACTIONS:
                        await ws.send_json(
                            protocol.error_message(f"unknown action: {action}")
                        )
                        continue
                    params = {k: v for k, v in msg.items() if k != "action"}
                    await state.apply_and_broadcast(action, params)
                except WebSocketDisconnect:
                    raise
                except Exception as exc:  # bad params / reducer error
                    await ws.send_json(protocol.error_message(str(exc)))
        except WebSocketDisconnect:
            pass
        finally:
            state.manager.disconnect(ws)
            try:
                await state.broadcast_state()
            except Exception:
                pass

    # ---- REST: import ----------------------------------------------------
    async def _import_bytes(data: bytes, filename: str | None) -> dict:
        if state.controller.session is None:
            raise HTTPException(status_code=409, detail="no project open")
        suffix = os.path.splitext(filename or "upload")[1]
        fd, tmp_path = tempfile.mkstemp(suffix=suffix)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            try:
                result = import_audio(
                    state.controller.session,
                    tmp_path,
                    name=os.path.splitext(filename or "")[0] or None,
                )
            except AudioImportError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass

        payload = {
            "status": result.status,
            "audioHash": result.audio_hash,
            "item": result.item.to_dict() if result.item else None,
            "matches": [m.to_dict() for m in result.matches],
        }
        if result.status == "new":
            await state.broadcast_state()
        return payload

    @app.post("/api/import")
    async def api_import(request: Request) -> dict:  # type: ignore[misc]
        # Parse the upload from the raw request rather than a typed ``UploadFile``
        # parameter: under ``from __future__ import annotations`` FastAPI cannot
        # build a validator for the forward-ref'd UploadFile annotation.
        require_auth(request)
        content_type = request.headers.get("content-type", "")
        if content_type.startswith("multipart/form-data"):
            form = await request.form()
            upload = form.get("file")
            if upload is None or isinstance(upload, str):
                raise HTTPException(status_code=400, detail="missing 'file' upload")
            data = await upload.read()
            filename = getattr(upload, "filename", None)
        else:
            # Raw request body; filename via ``?filename=`` query.
            data = await request.body()
            filename = request.query_params.get("filename")
        return await _import_bytes(data, filename)

    @app.post("/api/import/clone")
    async def api_import_clone(
        request: Request, _auth: None = Depends(require_auth)
    ) -> dict:
        if state.controller.session is None:
            raise HTTPException(status_code=409, detail="no project open")
        body = await request.json()
        audio_hash = body.get("audioHash")
        if not audio_hash:
            raise HTTPException(status_code=400, detail="missing 'audioHash'")
        try:
            item = add_clone(
                state.controller.session, audio_hash, body.get("name", "clone")
            )
        except (AudioImportError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        await state.broadcast_state()
        return item.to_dict()

    @app.post("/api/import/youtube")
    async def api_import_youtube(request: Request) -> Response:
        # Download bestaudio via yt-dlp, then funnel the temp file through the
        # same import_audio() pipeline as /api/import. Streams NDJSON progress
        # (one JSON object per line) so the client can drive a progress bar.
        require_auth(request)
        if state.controller.session is None:
            raise HTTPException(status_code=409, detail="no project open")
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="invalid JSON body")
        url = (body or {}).get("url")
        if not url or not isinstance(url, str):
            raise HTTPException(status_code=400, detail="missing 'url'")

        session = state.controller.session

        async def gen():
            async for ev in youtube_import_events(
                session, url, broadcast=state.broadcast_state
            ):
                yield (json.dumps(ev) + "\n").encode("utf-8")

        return StreamingResponse(gen(), media_type="application/x-ndjson")

    @app.get("/api/audio/{audio_hash}")
    async def api_audio(audio_hash: str, request: Request) -> Response:
        require_auth(request)
        session = state.controller.session
        if session is None:
            raise HTTPException(status_code=409, detail="no project open")
        try:
            path = session.audio_path(audio_hash)
        except ValueError:
            raise HTTPException(status_code=404, detail="audio not found")
        if not os.path.isfile(path):
            raise HTTPException(status_code=404, detail="audio not found")
        # Optional transcode: iOS Safari's WebAudio cannot decode FLAC, so remote
        # iPhones/iPads request ?format=wav and get 16-bit PCM instead.
        fmt = request.query_params.get("format")
        if fmt is None or fmt == "flac":
            return FileResponse(path, media_type="audio/flac")
        if fmt != "wav":
            raise HTTPException(status_code=400, detail="unsupported format")
        fd, tmp_path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        try:
            await asyncio.to_thread(transcode_to_wav, path, tmp_path)
        except ExportError as exc:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            raise HTTPException(status_code=500, detail=str(exc))
        return FileResponse(
            tmp_path,
            media_type="audio/wav",
            background=BackgroundTask(_safe_remove, tmp_path),
        )

    @app.get("/api/export/{library_item_id}")
    async def api_export(library_item_id: str, request: Request) -> Response:
        require_auth(request)
        session = state.controller.session
        if session is None:
            raise HTTPException(status_code=409, detail="no project open")
        fmt = (request.query_params.get("format") or "").lower()
        if fmt not in EXPORT_FORMATS:
            raise HTTPException(status_code=400, detail="unsupported format")
        item = session.show.library.get(library_item_id)
        if item is None:
            raise HTTPException(status_code=404, detail="library item not found")
        if item.type == "stop" or item.audio_hash is None:
            raise HTTPException(status_code=400, detail="item has no audio to export")
        if not session.has_audio(item.audio_hash):
            raise HTTPException(status_code=404, detail="audio not found")
        fd, tmp_path = tempfile.mkstemp(suffix=f".{fmt}")
        os.close(fd)
        try:
            await asyncio.to_thread(export_cue, session, item, fmt, tmp_path)
        except ExportError as exc:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            raise HTTPException(status_code=500, detail=str(exc))
        return FileResponse(
            tmp_path,
            media_type=media_type_for(fmt),
            headers={"Content-Disposition": content_disposition(item.name, fmt)},
            background=BackgroundTask(_safe_remove, tmp_path),
        )

    # ---- REST: devices / settings ---------------------------------------
    @app.get("/api/devices")
    async def api_devices(request: Request) -> list:
        require_auth(request)
        try:
            return AudioEngine.list_output_devices()
        except Exception:
            return []

    @app.get("/api/settings")
    async def api_get_settings(request: Request) -> dict:
        require_auth(request)
        return dict(state.config)

    @app.post("/api/settings")
    async def api_set_settings(request: Request) -> dict:
        require_auth(request)
        body = await request.json()
        allowed = set(DEFAULT_CONFIG.keys())
        for key, value in body.items():
            if key in allowed:
                state.config[key] = value
        save_config(state.config)
        if "outputDevice" in body:
            try:
                state.engine.set_default_device(state.config.get("outputDevice"))
            except Exception:
                pass
        if "masterDb" in body:
            try:
                state.engine.set_master_gain(float(state.config.get("masterDb", 0.0) or 0.0))
            except Exception:
                pass
        await state.broadcast_state()
        return dict(state.config)

    # ---- REST: connection ------------------------------------------------
    @app.get("/api/connection")
    async def api_connection(request: Request) -> dict:
        require_auth(request)
        return connection.connection_info(
            int(state.config.get("port", 7070)), state.config.get("pin") or None
        )

    # ---- REST: ffmpeg (version status / update / dismiss) ----------------
    def _ffmpeg_status_payload() -> dict:
        info = ffmpeg_util.get_update_info()
        prov = ffmpeg_util.get_provision_state()
        dismissed = state.config.get("ffmpegDismissedVersion")
        return {
            "present": ffmpeg_util.is_available(),
            "version": info["installed"],
            "latest": info["latest"],
            "updateAvailable": ffmpeg_util.update_available(dismissed),
            "phase": prov["phase"],        # idle | downloading | ready | error
            "percent": prov["percent"],
            "downloaded": prov["downloaded"],
            "total": prov["total"],
            "error": prov["error"],
        }

    @app.get("/api/ffmpeg/status")
    async def api_ffmpeg_status(request: Request) -> dict:
        require_auth(request)
        return _ffmpeg_status_payload()

    @app.post("/api/ffmpeg/update")
    async def api_ffmpeg_update(request: Request) -> dict:
        require_auth(request)
        started = ffmpeg_util.start_update()
        return {"started": started, **_ffmpeg_status_payload()}

    @app.post("/api/ffmpeg/dismiss")
    async def api_ffmpeg_dismiss(request: Request) -> dict:
        require_auth(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        # "Don't show again for this version" persists the dismissed version;
        # a plain dismiss is handled client-side (nothing to store).
        if (body or {}).get("forever"):
            latest = ffmpeg_util.get_update_info().get("latest")
            if latest:
                state.config["ffmpegDismissedVersion"] = latest
                save_config(state.config)
        return _ffmpeg_status_payload()

    # ---- REST: application update (GitHub Releases) -----------------------
    def _update_status_payload() -> dict:
        info = update_util.get_check_info()
        ap = update_util.get_apply_state()
        return {
            "current": info["current"],
            "latest": info["latest"],
            "url": info["url"],              # release page for manual download
            "updateAvailable": update_util.update_available(),
            "canApply": update_util.can_apply(),
            "checkEnabled": bool(state.config.get("checkForUpdates", True)),
            "checked": info["checked"],
            "phase": ap["phase"],            # idle | downloading | restarting | error
            "percent": ap["percent"],
            "downloaded": ap["downloaded"],
            "total": ap["total"],
            "error": ap["error"] or info["error"],
        }

    @app.get("/api/update/status")
    async def api_update_status(request: Request) -> dict:
        require_auth(request)
        return _update_status_payload()

    @app.post("/api/update/check")
    async def api_update_check(request: Request) -> dict:
        # Explicit operator action: check even when the periodic check is off.
        require_auth(request)
        await asyncio.to_thread(update_util.check_now)
        return _update_status_payload()

    @app.post("/api/update/apply")
    async def api_update_apply(request: Request) -> dict:
        require_auth(request)
        if not update_util.can_apply():
            raise HTTPException(
                status_code=409,
                detail="not running a packaged build -- update from source with git",
            )
        if not update_util.update_available():
            raise HTTPException(status_code=409, detail="no update available")

        def _request_shutdown() -> None:
            # Runs on the update worker THREAD. A graceful stop waits for every
            # open connection, and the operator's browser keeps its WebSocket
            # open (it is busy polling for the restart) -- so close all client
            # sockets first, then ask the serving loop to exit. The launcher's
            # timeout_graceful_shutdown is the backstop for anything that still
            # lingers.
            if state.loop is not None:
                try:
                    asyncio.run_coroutine_threadsafe(
                        state.manager.close_all(), state.loop
                    ).result(timeout=5)
                except Exception:
                    pass
            srv = state.uvicorn_server
            if srv is not None:
                srv.should_exit = True

        started = update_util.start_apply(_request_shutdown)
        return {"started": started, **_update_status_payload()}

    # ---- REST: project lifecycle ----------------------------------------
    # Serializes project-mutating REST endpoints (currently upload) so the
    # dedup-then-save sequence is atomic and can't overwrite a project a
    # concurrent request just created.
    _project_lock = asyncio.Lock()

    def _work_dir_for(name: str) -> str:
        WORK_ROOT.mkdir(parents=True, exist_ok=True)
        safe = "".join(c for c in name if c.isalnum() or c in ("-", "_", " ")).strip()
        # Suffix a short hash of the *full* name so two distinct project names
        # that sanitize to the same string (e.g. "Demo (2)" vs "Demo 2", or a
        # reserved name like "CON") never share a working folder -- otherwise
        # ProjectSession.open would extract one show on top of the other's live
        # audio and cross-contaminate them.
        digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
        return str(WORK_ROOT / f"{safe or 'untitled'}-{digest}")

    def _project_path(name: str) -> str:
        name = validate_project_name(name)
        PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
        return str(PROJECTS_DIR / f"{name}.cueforge")

    def _record_last_project(name: str) -> None:
        """Persist ``name`` as the project to reopen on next startup."""
        state.config["lastProject"] = name
        try:
            save_config(state.config)
        except Exception:
            pass

    @app.post("/api/project/new")
    async def api_project_new(request: Request) -> dict:
        require_auth(request)
        body = await request.json()
        name = validate_project_name(body.get("name") or "Untitled")
        session = ProjectSession.create_new(_work_dir_for(name), name)
        state.controller.set_session(session)
        await state.broadcast_state()
        return {"name": name}

    @app.post("/api/project/open")
    async def api_project_open(request: Request) -> dict:
        require_auth(request)
        body = await request.json()
        name = validate_project_name(body.get("name") or "")
        path = _project_path(name)
        if not os.path.isfile(path):
            raise HTTPException(status_code=404, detail="project not found")
        try:
            session = ProjectSession.open(path, _work_dir_for(name))
        except (zipfile.BadZipFile, json.JSONDecodeError, KeyError, ValueError) as exc:
            # Corrupt file or a show saved by a newer CueForge (format version).
            raise HTTPException(status_code=400, detail=f"cannot open project: {exc}")
        state.controller.set_session(session)
        _record_last_project(name)
        state.render_manager.schedule_dirty_all()
        await state.broadcast_state()
        return {"name": name}

    @app.post("/api/project/rename")
    async def api_project_rename(request: Request) -> dict:
        require_auth(request)
        session = state.controller.session
        if session is None:
            raise HTTPException(status_code=409, detail="no project open")
        body = await request.json()
        name = validate_project_name(body.get("name") or "")
        old_name = session.show.name
        session.show.name = name
        session.autosave()
        # If this project had already been saved under its old name, move the
        # saved .cueforge to the new name so the quick-switch list stays correct.
        if old_name and old_name != name:
            old_path = _project_path(old_name)
            if os.path.isfile(old_path):
                session.save_as(_project_path(name))
                try:
                    os.remove(old_path)
                except OSError:
                    pass
                _record_last_project(name)
        await state.broadcast_state()
        return {"name": name}

    @app.post("/api/project/save")
    async def api_project_save(request: Request) -> dict:
        require_auth(request)
        session = state.controller.session
        if session is None:
            raise HTTPException(status_code=409, detail="no project open")
        name = session.show.name
        path = _project_path(name)
        session.save_as(path)
        _record_last_project(name)
        return {"name": name, "path": path}

    @app.get("/api/projects")
    async def api_projects(request: Request) -> list:
        require_auth(request)
        PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
        return [
            {"name": p.stem, "path": str(p)}
            for p in sorted(PROJECTS_DIR.glob("*.cueforge"))
        ]

    def _unique_project_name(base: str) -> str:
        """A project name not already saved, appending ' (2)', ' (3)', ... on
        collision so an uploaded show never clobbers an existing one."""
        base = base.strip() or "Untitled"
        if not os.path.exists(_project_path(base)):
            return base
        n = 2
        while os.path.exists(_project_path(f"{base} ({n})")):
            n += 1
        return f"{base} ({n})"

    @app.get("/api/project/download")
    async def api_project_download(request: Request) -> Response:
        """Download the current project as a portable ``.cueforge`` file so the
        operator can carry it to another machine (USB stick etc.)."""
        require_auth(request)
        session = state.controller.session
        if session is None:
            raise HTTPException(status_code=409, detail="no project open")
        name = session.show.name or "Untitled"
        fd, tmp_path = tempfile.mkstemp(suffix=".cueforge")
        os.close(fd)
        try:
            await asyncio.to_thread(session.save_as, tmp_path)
        except Exception as exc:  # noqa: BLE001 -- surface as a clean 500
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            raise HTTPException(status_code=500, detail=f"could not package project: {exc}")
        return FileResponse(
            tmp_path,
            media_type="application/octet-stream",
            headers={"Content-Disposition": content_disposition(name, "cueforge")},
            background=BackgroundTask(_safe_remove, tmp_path),
        )

    @app.post("/api/project/upload")
    async def api_project_upload(request: Request) -> dict:  # type: ignore[misc]
        """Load a ``.cueforge`` uploaded from the client device, storing it as a
        saved project (auto-renamed on name collision) and opening it."""
        require_auth(request)
        content_type = request.headers.get("content-type", "")
        if content_type.startswith("multipart/form-data"):
            form = await request.form()
            upload = form.get("file")
            if upload is None or isinstance(upload, str):
                raise HTTPException(status_code=400, detail="missing 'file' upload")
            data = await upload.read()
            filename = getattr(upload, "filename", None)
        else:
            data = await request.body()
            filename = request.query_params.get("filename")
        if not data:
            raise HTTPException(status_code=400, detail="empty upload")

        stem = os.path.splitext(os.path.basename(filename or "Untitled"))[0]
        safe_stem = "".join(c for c in stem if c.isalnum() or c in " -_()").strip()
        if not safe_stem:
            safe_stem = "Untitled"
        if safe_stem.upper() in _WIN_RESERVED:
            safe_stem += "_"

        fd, tmp_path = tempfile.mkstemp(suffix=".cueforge")
        os.close(fd)
        try:
            with open(tmp_path, "wb") as fh:
                fh.write(data)
            # Validate it is a real .cueforge BEFORE extracting or touching any
            # project state, so a bad upload can't leave a partial work folder
            # and a genuine server fault isn't mislabelled as a 400.
            if not zipfile.is_zipfile(tmp_path):
                raise HTTPException(status_code=400, detail="not a valid .cueforge file")
            with zipfile.ZipFile(tmp_path) as zf:
                if "show.json" not in zf.namelist():
                    raise HTTPException(
                        status_code=400,
                        detail="not a valid .cueforge file (missing show.json)",
                    )
            # Choose the name and persist under the lock so a concurrent upload
            # can't pick the same name and overwrite what we just saved.
            async with _project_lock:
                name = _unique_project_name(safe_stem)
                work_dir = _work_dir_for(name)
                try:
                    session = await asyncio.to_thread(
                        ProjectSession.open, tmp_path, work_dir
                    )
                except (zipfile.BadZipFile, json.JSONDecodeError, KeyError, ValueError) as exc:
                    shutil.rmtree(work_dir, ignore_errors=True)  # no residue
                    raise HTTPException(
                        status_code=400, detail=f"not a valid .cueforge file: {exc}"
                    )
                session.show.name = name
                await asyncio.to_thread(session.save_as, _project_path(name))
                state.controller.set_session(session)
                _record_last_project(name)
                state.render_manager.schedule_dirty_all()
            await state.broadcast_state()
            return {"name": name}
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    return app


# Module-level app for ``uvicorn cueforge.server.app:app``.
app = create_app()
