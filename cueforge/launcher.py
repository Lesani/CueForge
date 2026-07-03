# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Lesani. See LICENSE for details.
"""Console launcher for CueForge (access / networking).

Responsible for:
  1. Ensuring a PIN exists in the server config before the server starts, so
     remote clients (and ``GET /api/connection``) always see one.
  2. Resolving host/port (env override -> config -> default 7070).
  3. Drawing a console dashboard: ASCII-art logo + live stats table on the
     left, right-aligned join QR with the local/LAN URLs and PIN on the
     right. Redrawn in place every 2 s via ANSI cursor-home (VT mode); on
     consoles without VT support it degrades to a single static draw.
  4. Best-effort opening the default browser to the local UI.
  5. Running the FastAPI app with uvicorn until Ctrl+C.

All logic lives here (not in ``__main__.py``) so it stays import-safe under
PyInstaller: importing this module must never have side effects beyond normal
class/function definitions -- everything happens inside :func:`main`.
"""

from __future__ import annotations

import os
import secrets
import sys
import webbrowser

from cueforge import __version__, ffmpeg_util, update_util, ytdlp_util

BANNER_WIDTH = 60          # minimum rule width; the layout may be wider

# figlet "standard" font, pre-rendered so there is no runtime dependency.
# All lines padded to equal width (raw strings must not end in a backslash).
_LOGO = [
    r"  ____           _____                    ",
    r" / ___|   _  ___|  ___|__  _ __ __ _  ___ ",
    "| |  | | | |/ _ \\ |_ / _ \\| '__/ _` |/ _ \\ ",
    r"| |__| |_| |  __/  _| (_) | | | (_| |  __/",
    r" \____\__,_|\___|_|  \___/|_|  \__, |\___|",
    r"                               |___/      ",
]
_LOGO_TAGLINE = f"audio cue server  v{__version__}"
_COLUMN_GAP = 2


def _configure_console_utf8() -> None:
    """Best-effort: make the console handle the QR's Unicode block glyphs.

    The console QR (qrcode.print_ascii) uses half/full-block chars (e.g. U+2588)
    that crash on a legacy Windows cp1252 console. Switch the code page to UTF-8
    and reconfigure stdout so it renders on modern terminals and never crashes on
    older ones (where the always-printed URL + PIN and the in-app QR are the
    fallback).
    """
    if os.name == "nt":
        try:
            os.system("chcp 65001 >nul 2>&1")
        except Exception:
            pass
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _enable_vt() -> bool:
    """Enable ANSI escape processing; True if in-place redraws are possible.

    Windows Terminal has VT on by default but legacy conhost needs
    ENABLE_VIRTUAL_TERMINAL_PROCESSING. Returns False for pipes/redirects
    (GetConsoleMode fails / not a tty) so VT codes never leak into logs.
    """
    if os.name != "nt":
        try:
            return sys.stdout.isatty()
        except Exception:
            return False
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:
        return False


def _generate_pin() -> str:
    """A fresh 4-digit numeric PIN, e.g. "0483"."""
    return f"{secrets.randbelow(10000):04d}"


def _ffmpeg_status() -> str:
    """Dashboard ffmpeg line -- shown only while a download is in progress (or
    on failure). Empty once ready/available so the row disappears."""
    st = ffmpeg_util.get_provision_state()
    phase = st["phase"]
    if phase == "downloading":
        got = st["downloaded"] / (1024 * 1024)
        if st["total"]:
            total = st["total"] / (1024 * 1024)
            return f"downloading {st['percent']}% ({got:.0f}/{total:.0f} MB)"
        return f"downloading {got:.0f} MB"
    if phase == "error":
        return "download failed -- audio import unavailable"
    return ""


def _ensure_ffmpeg() -> None:
    """ffmpeg is required for audio import; kick off a background download if
    none is available so it lands while the server boots. When one is already
    present, check for a newer release in the background so the UI can offer an
    update."""
    # Sweep any orphaned partial downloads left by a previously killed run so
    # the cache dir stays clean, whether or not we download this boot.
    ffmpeg_util.cleanup_partials()
    ytdlp_util.cleanup_partials()
    if ffmpeg_util.is_available():
        ffmpeg_util.check_versions_in_background()
    else:
        ffmpeg_util.provision_in_background()


def _resolve_port(cfg: dict) -> int:
    """Port precedence: ``CUEFORGE_PORT`` env -> config["port"] -> 7070."""
    env = os.environ.get("CUEFORGE_PORT")
    if env:
        try:
            return int(env)
        except ValueError:
            pass
    try:
        return int(cfg.get("port") or 7070)
    except (TypeError, ValueError):
        return 7070


def _ensure_pin(app_module) -> str:
    """Ensure ``config["pin"]`` is a non-empty numeric PIN; persist if generated.

    Mutates ``app_module.app.state.cf.config`` in place (the exact dict the
    running FastAPI app reads for ``/api/settings``, ``/api/connection``, and
    auth) so the server never observes a stale in-memory config after we
    write a fresh PIN to disk.
    """
    cf = app_module.app.state.cf
    cfg = cf.config
    pin = str(cfg.get("pin") or "").strip()
    if not pin:
        pin = _generate_pin()
        cfg["pin"] = pin
        app_module.save_config(cfg)
    return pin


def _qr_lines(join_url: str) -> list[str]:
    try:
        import qrcode
    except Exception as exc:  # pragma: no cover - optional dependency missing
        return [f"(QR unavailable: {exc})"]
    try:
        import io

        qr = qrcode.QRCode(border=1)
        qr.add_data(join_url)
        qr.make(fit=True)
        buf = io.StringIO()
        qr.print_ascii(out=buf, invert=True)
        return buf.getvalue().splitlines()
    except Exception as exc:  # pragma: no cover - best-effort console art
        return [f"(QR render failed: {exc})"]


def _build_screen(ctx: dict, stats: dict | None, term_w: int) -> list[str]:
    """The full console frame as a list of lines.

      logo (ASCII art)   |   Scan to join from a tablet:
      tagline            |   [QR code]           (right-aligned)
      stats table        |   Local / Network / PIN
      --------------------------------------------------
      Running -- press Ctrl+C to stop.

    ``stats`` holds pre-formatted display strings (see the stats thread);
    None renders "-" placeholders until the first tick.
    """
    usable = max(BANNER_WIDTH, term_w - 1)

    left = [" " + ln for ln in _LOGO]
    left += ["", f"  {_LOGO_TAGLINE}", ""]
    rows = [
        ("Uptime", stats["uptime"] if stats else "-"),
        ("Clients", stats["clients"] if stats else "-"),
        ("Voices", stats["voices"] if stats else "-"),
        ("CPU", stats["cpu"] if stats else "-"),
        ("Memory", stats["mem"] if stats else "-"),
    ]
    left += [f"  {label:<9} {value}" for label, value in rows]
    ff = ctx.get("ffmpeg")
    if ff:
        left.append(f"  {'FFmpeg':<9} {ff}")

    info = [
        f"Local     {ctx['local_url']}",
        f"Network   {ctx['network_url']}",
        f"PIN       {ctx['pin_display']}",
    ]
    info_w = max(len(ln) for ln in info)
    right = ["Scan to join from a tablet:"]
    right += ctx["qr"]
    right += [""]
    # Pad the info rows to a common width so the label column stays aligned
    # when the block as a whole is right-aligned.
    right += [ln.ljust(info_w) for ln in info]

    left_w = max(len(ln) for ln in left)
    lines = []
    for i in range(max(len(left), len(right))):
        l = left[i] if i < len(left) else ""
        r = right[i] if i < len(right) else ""
        if r:
            # Flush the right block to the terminal edge, but never closer
            # than _COLUMN_GAP to the left column (narrow consoles).
            pad = max(usable - len(r), left_w + _COLUMN_GAP)
            lines.append(l.ljust(pad) + r)
        else:
            lines.append(l)
    lines.append("-" * usable)
    lines.append("  Running -- press Ctrl+C to stop.")
    return lines


def _draw_screen(ctx: dict, stats: dict | None, vt: bool, first: bool = False) -> None:
    """Draw (or redraw in place) the console frame.

    With VT: cursor-home + per-line erase (no full clear) so the 2 s refresh
    does not flicker. Without VT only the first draw happens -- the frame
    stays static and the console title still carries the live stats.
    """
    import shutil

    term_w = shutil.get_terminal_size((80, 24)).columns
    lines = _build_screen(ctx, stats, term_w)
    if vt:
        prefix = "\x1b[2J\x1b[H" if first else "\x1b[H"
        body = "".join(line + "\x1b[K\n" for line in lines)
        sys.stdout.write(prefix + body + "\x1b[0J")
    elif first:
        sys.stdout.write("\n".join(lines) + "\n")
    # Flush so the frame shows immediately even when stdout is block-buffered.
    sys.stdout.flush()


def _start_dashboard_thread(state, port: int, ctx: dict, vt: bool) -> None:
    """Redraw the console frame in place every 2 s (full-frame VT redraw):
    live server stats plus the ffmpeg download/provision status, mirrored into
    the console title on Windows so the booth operator can glance at load even
    when the window is minimized. psutil is optional -- without it the cpu/mem
    fields read "n/a" but the frame (and ffmpeg progress) still refreshes.
    Best-effort throughout: a bad tick is skipped, never takes down the server.
    """
    import threading
    import time

    try:
        import psutil

        proc = psutil.Process()
        proc.cpu_percent()  # prime the counter; first reading is always 0.0
    except Exception:
        proc = None

    started = time.monotonic()

    def fmt_uptime(seconds: float) -> str:
        s = int(seconds)
        return f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}"

    def loop() -> None:
        while True:
            time.sleep(2)
            try:
                ctx["ffmpeg"] = _ffmpeg_status()
                if proc is not None:
                    cpu = proc.cpu_percent()
                    mem_mb = proc.memory_info().rss / (1024 * 1024)
                else:
                    cpu = mem_mb = None
                clients = state.manager.count
                st = state.engine.get_status()
                voices = len(st.get("backgrounds") or [])
                if st.get("normal"):
                    voices += 1
                if st.get("audition"):
                    voices += 1
                stats = {
                    "uptime": fmt_uptime(time.monotonic() - started),
                    "clients": str(clients),
                    "voices": str(voices),
                    "cpu": f"{cpu:.0f} %" if cpu is not None else "n/a",
                    "mem": f"{mem_mb:.0f} MB" if mem_mb is not None else "n/a",
                }
                _draw_screen(ctx, stats, vt)
                if os.name == "nt" and proc is not None:
                    try:
                        import ctypes

                        ctypes.windll.kernel32.SetConsoleTitleW(
                            f"CueForge :{port} - {clients} clients"
                            f" | {voices} voices | cpu {cpu:.0f}%"
                            f" | mem {mem_mb:.0f} MB"
                        )
                    except Exception:
                        pass
            except Exception:
                pass  # keep the loop alive; a bad tick just skips one update

    threading.Thread(target=loop, name="cueforge-dashboard", daemon=True).start()


def main() -> None:
    import uvicorn

    from cueforge.server import app as app_module
    from cueforge.server import connection

    _configure_console_utf8()
    _ensure_ffmpeg()
    update_util.cleanup_leftovers()
    pin = _ensure_pin(app_module)
    cfg = app_module.app.state.cf.config
    port = _resolve_port(cfg)
    host = "0.0.0.0"

    # Periodic app-update check (GitHub Releases), gated on the live setting so
    # toggling "check for updates" in the UI takes effect without a restart.
    update_util.start_periodic_checks(
        lambda: bool(cfg.get("checkForUpdates", True))
    )

    # Keep the in-memory config's "port" in sync with what we actually bind so
    # GET /api/connection reports the real port for this session (e.g. when
    # CUEFORGE_PORT overrides the configured value). Session-only: do not
    # persist an env-var override to disk.
    cfg["port"] = port

    try:
        lan_ip = connection.detect_lan_ip()
    except Exception:
        lan_ip = "127.0.0.1"
    join_url = connection.join_url(port, pin or None, lan_ip)

    vt = _enable_vt()
    ctx = {
        "local_url": f"http://localhost:{port}/",
        "network_url": f"http://{lan_ip}:{port}/",
        "pin_display": pin if pin else "(open access -- no PIN set)",
        "qr": _qr_lines(join_url),
        "ffmpeg": _ffmpeg_status(),
    }
    _draw_screen(ctx, None, vt, first=True)

    if os.environ.get("CUEFORGE_NO_BROWSER") != "1":
        try:
            webbrowser.open(f"http://localhost:{port}/")
        except Exception:
            pass  # best-effort only; the console banner is the fallback

    _start_dashboard_thread(app_module.app.state.cf, port, ctx, vt)

    # Build the server explicitly (instead of uvicorn.run) so the self-update
    # endpoint can reach it and request a graceful stop via should_exit.
    server = uvicorn.Server(
        uvicorn.Config(
            app_module.app, host=host, port=port, log_level="warning",
            # Backstop for the self-update restart: never wait forever on a
            # lingering connection once should_exit is requested.
            timeout_graceful_shutdown=5,
        )
    )
    app_module.app.state.cf.uvicorn_server = server
    server.run()

    # A successful self-update already swapped the exe on disk and stopped the
    # server; hand over to the new build now that the port is free.
    if update_util.restart_pending():
        update_util.spawn_replacement()


if __name__ == "__main__":
    main()
