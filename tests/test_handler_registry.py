"""The WebSocket handler registry (app/handlers/registry.py) and the dispatch
semantics it replaced a hand-written elif chain with."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from app.handlers import HANDLERS, dispatch, review_flow
from app.handlers.registry import HandlerSpec
from app.handlers.settings import handle_set_settings
from app.handlers.voice import handle_speak_turn
from app.services.diff_service import Hunk
from app.web import runtime
from app.web.config import CONFIG
from app.web.session import Session
from app.web.speech import try_speak

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

# Sent by the VS Code extension only: its webview can't open the microphone,
# so the server records for it (app/handlers/recording.py).
NON_BROWSER_MESSAGES = {"start_recording", "stop_recording"}


def _frontend_js() -> str:
    """Every frontend .js file's text. Globs rather than naming one file:
    the UI is split across static/js/, and a send() call in a module this
    forgot to list would silently vanish from the comparison below instead
    of failing it. Joined on a newline so no file's last token can fuse
    with the next file's first one."""
    return "\n".join(path.read_text(encoding="utf-8") for path in sorted(STATIC_DIR.rglob("*.js")))


class FakeSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)

    def types(self) -> list[str]:
        return [m["type"] for m in self.sent]


def _hunks(n: int) -> list[Hunk]:
    return [Hunk(index=i, file_path=f"f{i}.py", header="@@ -1,1 +1,1 @@", lines=["+x"]) for i in range(n)]


def test_every_frontend_message_has_a_handler():
    sent = set(re.findall(r'\bsend\("([a-z_]+)"', _frontend_js()))
    assert sent, 'found no send("...") calls — has the frontend\'s send() changed shape?'
    assert sent | NON_BROWSER_MESSAGES == set(HANDLERS)
    assert not sent & NON_BROWSER_MESSAGES


def test_unknown_and_malformed_types_get_an_error_not_a_crash(tmp_path):
    ws = FakeSocket()
    session = Session([], None, str(tmp_path))

    async def run():
        await dispatch(ws, session, "no_such_message", {})
        await dispatch(ws, session, ["unhashable"], {})
        await dispatch(ws, session, None, {})

    asyncio.run(run())
    assert ws.types() == ["error", "error", "error"]


def test_cancels_and_background_flags(tmp_path, monkeypatch):
    ws = FakeSocket()
    session = Session([], None, str(tmp_path))
    ran: list[str] = []

    async def record(ws, session, payload):
        ran.append(payload["name"])

    monkeypatch.setitem(HANDLERS, "t_inline", HandlerSpec(record, cancels=False, background=False))
    monkeypatch.setitem(HANDLERS, "t_background", HandlerSpec(record, cancels=True, background=True))

    async def run():
        blocker = asyncio.create_task(asyncio.sleep(10))
        session.current_task = blocker

        await dispatch(ws, session, "t_inline", {"name": "inline"})
        assert ran == ["inline"] and session.current_task is blocker  # awaited, nothing cancelled

        await dispatch(ws, session, "t_background", {"name": "background"})
        assert session.current_task is not blocker and ran == ["inline"]  # scheduled, not yet run
        await session.current_task
        await asyncio.sleep(0)
        assert blocker.cancelled() and ran == ["inline", "background"]

    asyncio.run(run())


def test_rapid_next_clicks_each_advance(tmp_path, monkeypatch):
    """Why next/prev bump the index inline instead of registering as
    background: inside a task, a second click would cancel the first before
    its increment ran, and two clicks would move one hunk."""

    async def present(ws, session):
        await asyncio.sleep(0)

    monkeypatch.setattr(review_flow, "present_current_hunk", present)
    session = Session(_hunks(3), None, str(tmp_path))
    session.index = 0

    async def run():
        await dispatch(FakeSocket(), session, "next", {})
        await dispatch(FakeSocket(), session, "next", {})

    asyncio.run(run())
    assert session.index == 2


def _run_stale_prep_warning(tmp_path, monkeypatch, status: dict) -> FakeSocket:
    monkeypatch.setattr(review_flow, "context_status", lambda repo_path: status)
    ws = FakeSocket()
    asyncio.run(review_flow._warn_about_stale_prep(ws, Session([], None, str(tmp_path))))
    return ws


def test_prep_written_before_the_current_commit_is_warned_about(tmp_path, monkeypatch):
    """Stale prep is fed to the model as background fact. Measured on this
    repo: a call map written before a refactor named a module that had been
    deleted, and knew nothing of the package that replaced it."""
    ws = _run_stale_prep_warning(
        tmp_path,
        monkeypatch,
        {
            "call_map": {
                "present": True,
                "head_moved": True,
                "refresh_hint": "Ask Claude Code: use the call-map skill",
            },
        },
    )
    assert ws.types() == ["notice"]
    assert "call map" in ws.sent[0]["payload"]["message"]
    assert "use the call-map skill" in ws.sent[0]["payload"]["message"]


def test_a_missing_prep_file_is_not_warned_about(tmp_path, monkeypatch):
    """Narration runs fine without one, so "you could generate this" stays
    in the settings panel rather than interrupting a review."""
    ws = _run_stale_prep_warning(
        tmp_path,
        monkeypatch,
        {
            "call_map": {"present": False, "refresh_hint": "Ask Claude Code: use the call-map skill"},
            "project_overview": {"present": True, "head_moved": False, "refresh_hint": "x"},
        },
    )
    assert ws.sent == []


def test_tts_settings_change_reaches_the_next_spoken_turn(tmp_path, monkeypatch):
    """runtime.TTS is rebound by set_settings; a module that had done
    `from runtime import TTS` would keep speaking through the old client."""
    monkeypatch.setitem(CONFIG, "tts", dict(CONFIG["tts"]))
    monkeypatch.setattr(runtime, "TTS", runtime.TTS)
    old_client = runtime.TTS
    ws = FakeSocket()
    session = Session([], None, str(tmp_path))

    asyncio.run(handle_set_settings(ws, session, {"settings": {"tts": {"endpoint": "http://new-tts.invalid"}}}))
    assert runtime.TTS is not old_client and runtime.TTS.endpoint == "http://new-tts.invalid"

    monkeypatch.setattr(runtime.TTS, "synthesize", lambda text: b"audio")
    asyncio.run(try_speak(ws, session, "Hello there."))
    assert "audio_chunk" in ws.types()


def test_speak_turn_speaks_even_with_voice_output_off(tmp_path, monkeypatch):
    """The speaker button exists for reviewers who keep narration silent,
    so the voice preference must not gate it. Its clips go out as
    turn_audio_chunk, which the frontend files under the clicked turn."""
    monkeypatch.setattr(runtime.TTS, "synthesize", lambda text: b"audio")
    ws = FakeSocket()
    session = Session([], None, str(tmp_path))
    session.tts_enabled = False

    asyncio.run(handle_speak_turn(ws, session, {"text": "It retries three times."}))
    assert "turn_audio_chunk" in ws.types()
    assert "audio_chunk" not in ws.types()


WIRE_DOC = Path(__file__).resolve().parent.parent / "docs" / "wire-protocol.md"


def _documented_inbound() -> dict[str, str]:
    """{message: "module.function"} parsed from wire-protocol.md's inbound
    table alone — the outbound and shared-field tables use the same row
    shape, so this reads only the section between the two headings. A
    payload column listing alternatives escapes its own pipe; those are
    dropped before splitting so they can't look like cell boundaries."""
    text = WIRE_DOC.read_text(encoding="utf-8")
    section = text.split("## Browser \u2192 Python")[1].split("## Python \u2192 Browser")[0]
    rows: dict[str, str] = {}
    for line in section.splitlines():
        if not line.startswith("| `"):
            continue
        cells = [c.strip() for c in line.replace(chr(92) + "|", "/").strip("|").split("|")]
        rows[cells[0].strip("`")] = cells[2].strip("`")
    return rows


def test_wire_protocol_doc_matches_the_registry():
    """wire-protocol.md is the contract the frontend is written against,
    and it lives in a different file from every handler it describes — the
    arrangement that let the old server.py docstring fall six message types
    behind the code, three of them whole handlers nobody had documented at
    all. Handler names are checked too, so a rename can't leave the doc
    pointing at a function that no longer exists."""
    documented = _documented_inbound()
    assert documented, "parsed no rows — has wire-protocol.md's table changed shape?"
    assert set(documented) == set(HANDLERS), (
        f"undocumented: {sorted(set(HANDLERS) - set(documented))}; "
        f"documented but not registered: {sorted(set(documented) - set(HANDLERS))}"
    )
    assert documented == {
        msg: f"{spec.fn.__module__.rsplit('.', 1)[-1]}.{spec.fn.__name__}" for msg, spec in HANDLERS.items()
    }
