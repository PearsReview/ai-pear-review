"""server.py's WebSocket handshake guard.

The protocol is unauthenticated, so this check is the only thing standing
between a page in another tab and the full message surface (queue comments,
read files through Act Now, enumerate definitions). qa_agent/test_security.py
drives the real browser cases; these are the header combinations a browser
can be *made* to send, which Playwright cannot produce.
"""

from __future__ import annotations

import pytest

from app.server import _origin_is_trusted


class _FakeWebSocket:
    def __init__(self, **headers: str):
        self.headers = {k: v for k, v in headers.items() if v is not None}


@pytest.mark.parametrize(
    "origin,host",
    [
        ("http://127.0.0.1:8765", "127.0.0.1:8765"),
        ("http://localhost:8765", "localhost:8765"),
        ("http://[::1]:8765", "[::1]:8765"),
        ("http://LOCALHOST:8765", "localhost:8765"),  # browsers may differ in case
    ],
)
def test_the_app_s_own_page_is_accepted(origin: str, host: str):
    assert _origin_is_trusted(_FakeWebSocket(origin=origin, host=host))


@pytest.mark.parametrize(
    "origin,host",
    [
        ("http://evil.example", "127.0.0.1:8765"),  # another tab, plain CSWSH
        ("http://127.0.0.1:9999", "127.0.0.1:8765"),  # another local app
        (None, "127.0.0.1:8765"),  # no Origin at all
        ("http://127.0.0.1:8765", None),  # no Host at all
    ],
)
def test_anything_else_is_rejected(origin: str | None, host: str | None):
    assert not _origin_is_trusted(_FakeWebSocket(origin=origin, host=host))


@pytest.mark.parametrize(
    "hostname",
    ["evil.test", "rebind.example.com", "192.168.1.10"],
)
def test_dns_rebinding_is_rejected_even_though_origin_matches_host(hostname: str):
    """The attacker controls both headers: their page is served from
    evil.test, whose name then re-resolves to 127.0.0.1. Origin and Host
    agree, so equality alone would let it in — the name is what gives it
    away."""
    ws = _FakeWebSocket(origin=f"http://{hostname}:8765", host=f"{hostname}:8765")
    assert not _origin_is_trusted(ws)


def test_a_deliberately_network_bound_server_keeps_the_equality_check_only(monkeypatch):
    """Binding 0.0.0.0 is opt-in and warned about at startup. There, a real
    reviewer reaches the app by the machine's own hostname, so requiring a
    loopback Host would reject every legitimate connection."""
    monkeypatch.setattr("app.server.BOUND_TO_LOOPBACK", False)
    ws = _FakeWebSocket(origin="http://dev-box.lan:8765", host="dev-box.lan:8765")
    assert _origin_is_trusted(ws)
    assert not _origin_is_trusted(_FakeWebSocket(origin="http://evil.example", host="dev-box.lan:8765"))
