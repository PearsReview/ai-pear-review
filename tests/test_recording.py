"""Server-side push-to-talk (app/services/recorder.py, app/handlers/recording.py),
against a fake sounddevice — no real microphone in the default run."""

from __future__ import annotations

import asyncio
import base64
import io
import sys
import types
import wave
from typing import ClassVar

import pytest

from app.handlers import dispatch
from app.services.recorder import MAX_SECONDS, SAMPLE_RATE, Recorder, RecorderError
from app.services.voice_service import _audio_upload
from app.web.session import Session


class FakeStream:
    """Stands in for sounddevice.RawInputStream: feed() plays the part of
    PortAudio calling back with captured frames."""

    instances: ClassVar[list[FakeStream]] = []

    def __init__(self, samplerate, channels, dtype, callback):
        self.callback = callback
        self.closed = False
        FakeStream.instances.append(self)

    def start(self):
        pass

    def stop(self):
        pass

    def close(self):
        self.closed = True

    def feed(self, seconds: float) -> None:
        frames = int(seconds * SAMPLE_RATE)
        self.callback(b"\x01\x00" * frames, frames, None, None)


@pytest.fixture
def fake_sounddevice(monkeypatch):
    FakeStream.instances = []
    monkeypatch.setitem(sys.modules, "sounddevice", types.SimpleNamespace(RawInputStream=FakeStream))


class FakeSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)

    def types(self) -> list[str]:
        return [m["type"] for m in self.sent]


def _read_wav(data: bytes) -> tuple[int, int, int]:
    with wave.open(io.BytesIO(data), "rb") as wav:
        return wav.getframerate(), wav.getnchannels(), wav.getnframes()


def test_stop_returns_the_captured_audio_as_16k_mono_wav(fake_sounddevice):
    recorder = Recorder()
    recorder.start()
    FakeStream.instances[0].feed(0.5)
    FakeStream.instances[0].feed(0.25)
    wav = recorder.stop()
    assert _read_wav(wav) == (SAMPLE_RATE, 1, int(0.75 * SAMPLE_RATE))
    assert FakeStream.instances[0].closed and not recorder.recording


def test_frames_past_the_cap_are_dropped(fake_sounddevice):
    recorder = Recorder()
    recorder.start()
    FakeStream.instances[0].feed(MAX_SECONDS)
    FakeStream.instances[0].feed(1)
    assert _read_wav(recorder.stop())[2] == MAX_SECONDS * SAMPLE_RATE


def test_out_of_order_calls_raise(fake_sounddevice):
    recorder = Recorder()
    with pytest.raises(RecorderError):
        recorder.stop()
    recorder.start()
    with pytest.raises(RecorderError):
        recorder.start()
    recorder.cancel()
    assert not recorder.recording
    recorder.cancel()  # idle cancel is a no-op


def test_missing_sounddevice_is_a_recorder_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "sounddevice", None)  # makes the import raise ImportError
    with pytest.raises(RecorderError, match="sounddevice"):
        Recorder().start()


def test_handlers_round_trip_a_recording(fake_sounddevice, tmp_path):
    ws = FakeSocket()
    session = Session([], None, str(tmp_path))

    async def run():
        await dispatch(ws, session, "start_recording", {})
        FakeStream.instances[0].feed(1.0)
        await dispatch(ws, session, "stop_recording", {})

    asyncio.run(run())
    assert ws.types() == ["recording_state", "recording_state", "recording_result"]
    result = ws.sent[2]["payload"]
    assert result["mime_type"] == "audio/wav" and result["duration_seconds"] == 1.0
    assert _read_wav(base64.b64decode(result["audio_base64"]))[2] == SAMPLE_RATE


def test_a_too_short_recording_is_an_error_not_a_result(fake_sounddevice, tmp_path):
    ws = FakeSocket()
    session = Session([], None, str(tmp_path))

    async def run():
        await dispatch(ws, session, "start_recording", {})
        FakeStream.instances[0].feed(0.1)
        await dispatch(ws, session, "stop_recording", {})

    asyncio.run(run())
    assert ws.types() == ["recording_state", "recording_state", "error"]


def test_stop_without_start_is_an_error(tmp_path):
    ws = FakeSocket()
    asyncio.run(dispatch(ws, Session([], None, str(tmp_path)), "stop_recording", {}))
    assert ws.types() == ["error"]


def test_stt_upload_is_labelled_with_the_real_format(fake_sounddevice):
    recorder = Recorder()
    recorder.start()
    FakeStream.instances[0].feed(0.5)
    assert _audio_upload(recorder.stop())[2] == "audio/wav"
    assert _audio_upload(b"\x1aE\xdf\xa3 webm bytes")[2] == "audio/webm"
