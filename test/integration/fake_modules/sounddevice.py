"""A stand-in for the sounddevice package in the integration tests: put this folder
first on PYTHONPATH and the backend's recorder (app/services/recorder.py) records a
440 Hz tone instead of opening a real microphone.

Implements only what the recorder uses: RawInputStream with start/stop/close, calling
the callback from its own thread as PortAudio does."""

from __future__ import annotations

import math
import struct
import threading
import time

_BLOCK_SECONDS = 0.05


class RawInputStream:
    def __init__(self, samplerate, channels, dtype, callback):
        self._rate = int(samplerate)
        self._callback = callback
        self._running = False
        self._thread = None

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        frames = int(self._rate * _BLOCK_SECONDS)
        t = 0
        while self._running:
            block = b"".join(
                struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * (t + i) / self._rate))) for i in range(frames)
            )
            t += frames
            self._callback(block, frames, None, None)
            time.sleep(_BLOCK_SECONDS)

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=1)

    def close(self):
        self._running = False
