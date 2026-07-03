# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Lesani. See LICENSE for details.
"""LAN URL detection, PIN handling, and in-app QR generation.

Localhost is trusted (no PIN). Remote devices must present the PIN. The QR
encodes the LAN URL with the PIN as a query parameter for one-tap join.
"""

from __future__ import annotations

import base64
import io
import ipaddress
import socket
from urllib.parse import urlencode

LOCALHOST = {"127.0.0.1", "::1", "localhost"}


def detect_lan_ip() -> str:
    """Best-effort primary LAN IPv4 address of this machine.

    Uses a connected (but not transmitting) UDP socket so the OS picks the
    outbound interface. Falls back to ``127.0.0.1`` if nothing is reachable.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip


def is_local_address(host: str | None) -> bool:
    """Whether a client host string is loopback (trusted, no PIN required)."""
    if not host:
        return False
    host = host.strip()
    # Strip an IPv6 zone id / brackets if present.
    host = host.strip("[]").split("%", 1)[0]
    if host in LOCALHOST:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def local_url(port: int) -> str:
    """The loopback URL the server auto-opens on the booth machine."""
    return f"http://127.0.0.1:{port}/"


def lan_url(port: int, host: str | None = None) -> str:
    """The URL remote devices use to reach this server on the LAN."""
    host = host or detect_lan_ip()
    return f"http://{host}:{port}/"


def join_url(port: int, pin: str | None, host: str | None = None) -> str:
    """LAN URL with the PIN embedded as a ``?pin=`` query for one-tap join."""
    base = lan_url(port, host)
    if pin:
        base = f"{base}?{urlencode({'pin': pin})}"
    return base


def qr_data_url(payload: str) -> str | None:
    """Return a ``data:image/png;base64,...`` QR encoding ``payload``.

    Returns ``None`` if the ``qrcode`` dependency is unavailable so the caller
    can degrade gracefully rather than crash.
    """
    try:
        import qrcode
    except Exception:
        return None
    img = qrcode.make(payload)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{b64}"


def connection_info(port: int, pin: str | None, host: str | None = None) -> dict:
    """Assemble the ``GET /api/connection`` payload."""
    join = join_url(port, pin, host)
    return {
        "url": local_url(port),
        "lanUrl": lan_url(port, host),
        "pin": pin,
        "qr": qr_data_url(join),
    }
