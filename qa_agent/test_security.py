"""Regression test for S1 (see the code review): WebSocket connections
aren't covered by the same-origin policy the way fetch()/XHR are, so
app/server.py's websocket_endpoint needs its own explicit Origin check
(_origin_is_trusted) — without it, any page open in any other browser tab
could open this same ws://.../ws URL and drive the full protocol.

A raw socket handshake, not Playwright — Playwright's own page always
connects from the app's own origin by construction, so it can't simulate
a genuinely different one. This is exactly the "add both as qa_agent
tests" the original review's Verification section asked for.
"""

from __future__ import annotations

import socket
from urllib.parse import urlsplit


def _raw_ws_handshake(app_server: str, origin: str | None, host_header: str | None = None) -> str:
    parts = urlsplit(app_server)
    host_header = host_header or parts.netloc
    s = socket.create_connection((parts.hostname, parts.port), timeout=5)
    lines = [
        "GET /ws HTTP/1.1",
        f"Host: {host_header}",
        "Upgrade: websocket",
        "Connection: Upgrade",
        "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==",
        "Sec-WebSocket-Version: 13",
    ]
    if origin is not None:
        lines.append(f"Origin: {origin}")
    request = "\r\n".join(lines) + "\r\n\r\n"
    s.sendall(request.encode())
    try:
        response = s.recv(500).decode(errors="replace")
    finally:
        s.close()
    return response.splitlines()[0] if response else "(no response)"


def test_cross_origin_websocket_is_rejected(app_server):
    # An attacker page hosted anywhere else attempting to connect directly.
    status_line = _raw_ws_handshake(app_server, origin="http://evil.example.com")
    assert "403" in status_line


def test_missing_origin_websocket_is_rejected(app_server):
    # Real browsers always send Origin on a WS handshake — its absence
    # means either a spoofed/stripped header or a client this protocol
    # was never meant for. Reject either way (see _origin_is_trusted).
    status_line = _raw_ws_handshake(app_server, origin=None)
    assert "403" in status_line


def test_same_origin_websocket_is_accepted(app_server):
    parts = urlsplit(app_server)
    status_line = _raw_ws_handshake(app_server, origin=f"{parts.scheme}://{parts.netloc}")
    assert "101" in status_line
