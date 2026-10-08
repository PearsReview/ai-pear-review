"""The machine-readable fields a client acts on instead of a message's wording:
"event" on the notices that bracket a file read and confirm an Act Now apply,
and "source" on the errors a file read sends (docs/wire-protocol.md, "Fields
shared across messages"). The wording stays free to change; these must not."""

from __future__ import annotations

import asyncio

import pytest

from app.handlers import act_now, voice
from app.services.harness_service import ProposedChange
from app.web import runtime
from app.web.session import Session


class FakeSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)


class FakeTTS:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    def synthesize(self, text: str) -> bytes:
        if self.fail:
            raise voice.VoiceServiceError("down")
        return b"RIFF"


@pytest.fixture
def readme(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(voice, "read_current_file", lambda repo, path: "# Title\n\nSome words to read.\n")


def _speak(session: Session, payload: dict) -> list[dict]:
    ws = FakeSocket()
    asyncio.run(voice.handle_speak_file(ws, session, payload))
    return ws.sent


def test_a_read_is_bracketed_by_reading_events(tmp_path, monkeypatch, readme):
    monkeypatch.setattr(runtime, "TTS", FakeTTS())
    sent = _speak(Session([], None, str(tmp_path)), {"file_path": "README.md"})

    events = [m["payload"].get("event") for m in sent if m["type"] == "notice"]
    assert events == ["reading_started", "reading_finished"]
    assert any(m["type"] == "file_audio_chunk" for m in sent)


@pytest.mark.parametrize(
    ("payload", "tts_enabled", "fail"),
    [
        ({"file_path": "notes.txt"}, True, False),  # not markdown
        ({"file_path": "README.md"}, False, False),  # speech turned off
        ({"file_path": "README.md"}, True, True),  # speech service down partway
    ],
)
def test_every_error_of_a_read_names_its_source(tmp_path, monkeypatch, readme, payload, tts_enabled, fail):
    monkeypatch.setattr(runtime, "TTS", FakeTTS(fail=fail))
    session = Session([], None, str(tmp_path))
    session.tts_enabled = tts_enabled
    errors = [m["payload"] for m in _speak(session, payload) if m["type"] == "error"]

    assert errors
    assert all(e.get("source") == "speak_file" for e in errors)


def test_other_errors_carry_no_source():
    ws = FakeSocket()
    asyncio.run(runtime.send_error(ws, "Something else went wrong."))
    assert ws.sent == [{"type": "error", "payload": {"message": "Something else went wrong."}}]


def test_an_applied_change_says_so_with_its_files(tmp_path, monkeypatch):
    monkeypatch.setattr(act_now, "apply_changes", lambda repo, changes: None)

    async def no_refresh(ws, session):
        return None

    monkeypatch.setattr(act_now, "refresh_diff", no_refresh)
    session = Session([], None, str(tmp_path))
    session.pending_act_now = (ProposedChange("a.py", "x", "y"), ProposedChange("b.py", None, "z"))
    ws = FakeSocket()
    asyncio.run(act_now.handle_confirm_act_now(ws, session, {}))

    [notice] = [m["payload"] for m in ws.sent if m["type"] == "notice"]
    assert notice["event"] == "act_now_applied"
    assert notice["files"] == ["a.py", "b.py"]
