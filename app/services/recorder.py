"""Microphone recording on the machine the server runs on, for clients that
can't record themselves.

The browser UI never uses this — it records with MediaRecorder and sends
the audio in its reply. A VS Code webview can't: getUserMedia is blocked
there, so the extension asks the server to record instead, gets WAV bytes
back, and sends them in its reply exactly as the browser would. That keeps
this module to capture alone; it never transcribes, and never decides what
the audio is for.

`sounddevice` is an optional dependency (the `recording` extra in
pyproject.toml), imported on first use, so the web app installs and runs
without it.
"""

from __future__ import annotations

import io
import threading
import wave
from typing import Any

from .errors import ServiceError

# What Whisper resamples everything to anyway; recording at it keeps the
# upload small (~32 KB/s) without losing anything the transcriber uses.
SAMPLE_RATE = 16_000
_CHANNELS = 1
_SAMPLE_WIDTH_BYTES = 2  # int16

# A backstop for a press-to-talk that never gets its second press: frames
# past this are dropped rather than growing the buffer without bound.
MAX_SECONDS = 120


class RecorderError(ServiceError):
    """Raised when recording can't start or stop (no sounddevice, no input
    device, or start/stop called out of order)."""


class Recorder:
    """One recording at a time: start(), then stop() for the WAV bytes, or
    cancel() to discard. Frames arrive on PortAudio's own thread, hence the
    lock."""

    def __init__(self) -> None:
        self._stream: Any = None
        self._frames: list[bytes] = []
        self._frame_count = 0
        self._lock = threading.Lock()

    @property
    def recording(self) -> bool:
        return self._stream is not None

    def start(self) -> None:
        if self._stream is not None:
            raise RecorderError("Already recording.")
        try:
            import sounddevice  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RecorderError("Recording needs the sounddevice package (pip install sounddevice).") from exc
        except OSError as exc:  # the package is there but PortAudio isn't
            raise RecorderError(f"Recording unavailable: {exc}") from exc

        with self._lock:
            self._frames = []
            self._frame_count = 0
        try:
            stream = sounddevice.RawInputStream(
                samplerate=SAMPLE_RATE,
                channels=_CHANNELS,
                dtype="int16",
                callback=self._on_audio,
            )
            stream.start()
        except Exception as exc:  # PortAudio raises its own types, plus ValueError for a bad device
            raise RecorderError(f"Could not open the microphone: {exc}") from exc
        self._stream = stream

    def stop(self) -> bytes:
        """Ends the recording and returns it as a 16 kHz mono WAV file."""
        if self._stream is None:
            raise RecorderError("Not recording.")
        self._close()
        with self._lock:
            pcm = b"".join(self._frames)
            self._frames = []
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav:
            wav.setnchannels(_CHANNELS)
            wav.setsampwidth(_SAMPLE_WIDTH_BYTES)
            wav.setframerate(SAMPLE_RATE)
            wav.writeframes(pcm)
        return buffer.getvalue()

    def cancel(self) -> None:
        """Discards any recording in progress. Safe to call when idle."""
        if self._stream is not None:
            self._close()
        with self._lock:
            self._frames = []

    def _close(self) -> None:
        stream, self._stream = self._stream, None
        try:
            stream.stop()
            stream.close()
        except Exception:  # noqa: BLE001 — teardown: the frames are already captured, a close error changes nothing
            pass

    def _on_audio(self, data: Any, frames: int, time: Any, status: Any) -> None:
        with self._lock:
            if self._frame_count >= MAX_SECONDS * SAMPLE_RATE:
                return
            self._frames.append(bytes(data))
            self._frame_count += frames


def wav_duration_seconds(wav_bytes: bytes) -> float:
    with wave.open(io.BytesIO(wav_bytes), "rb") as wav:
        return wav.getnframes() / wav.getframerate()
