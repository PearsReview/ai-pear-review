"""Plays speech for the extension, outside any webview.

    python player.py

VS Code's webviews won't play sound until they have been clicked, so audio started
from the editor or the Explorer (Read Aloud) is played here instead, through the same
sounddevice package the backend records with.

One JSON object per line on stdin:
    {"cmd": "play", "id": 3, "wav": "<base64>"}   queue a clip (16-bit PCM WAV)
    {"cmd": "pause"} / {"cmd": "resume"} / {"cmd": "stop"} / {"cmd": "quit"}

One JSON object per line on stdout:
    {"event": "start", "id": 3}                       a clip began
    {"event": "progress", "id": 3, "fraction": 0.4}   a few times a second
    {"event": "done", "id": 3}                        a clip finished
    {"event": "idle"}                                 nothing left to play
    {"event": "error", "message": "..."}
"""

from __future__ import annotations

import base64
import json
import struct
import sys
import threading
import time
from array import array
from collections import deque

_BLOCK_SECONDS = 0.05
_PROGRESS_EVERY = 0.15

_PCM, _FLOAT, _EXTENSIBLE = 1, 3, 0xFFFE


def _little_endian(samples: array) -> bytes:
    """16-bit samples as the little-endian bytes the stream is given."""
    if sys.byteorder == "big":
        samples.byteswap()
    return samples.tobytes()


def to_pcm16(data: bytes) -> tuple[int, int, bytes]:
    """A WAV clip as (sample rate, channels, 16-bit little-endian PCM).

    Speech services differ: 16-bit is common, but 24- and 32-bit integer and
    32-bit float WAVs are real too, and the wave module reads neither float nor
    every extensible header. So the RIFF chunks are read here, and each sample
    is cut down to 16 bits. A data chunk whose size is unknown (streamed, 0 or
    0xFFFFFFFF) runs to the end of the clip."""
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("not a WAV clip")
    at, fmt, samples = 12, None, None
    while at + 8 <= len(data):
        kind, size = data[at : at + 4], struct.unpack_from("<I", data, at + 4)[0]
        body = at + 8
        if kind == b"fmt ":
            fmt = data[body : body + size]
        elif kind == b"data":
            end = len(data) if size in (0, 0xFFFFFFFF) else min(len(data), body + size)
            samples = data[body:end]
            break
        at = body + size + (size & 1)
    if fmt is None or len(fmt) < 16 or samples is None:
        raise ValueError("WAV clip without a format or data chunk")
    audio_format, channels, rate = struct.unpack_from("<HHI", fmt, 0)
    bits = struct.unpack_from("<H", fmt, 14)[0]
    if audio_format == _EXTENSIBLE and len(fmt) >= 26:
        audio_format = struct.unpack_from("<H", fmt, 24)[0]
    width = bits // 8
    samples = samples[: len(samples) - len(samples) % (width * channels or 1)]
    if audio_format == _PCM and bits == 16:
        pcm = samples
    elif audio_format == _PCM and bits == 8:
        pcm = _little_endian(array("h", ((b - 128) << 8 for b in samples)))
    elif audio_format == _PCM and bits in (24, 32):
        # Little-endian: a sample's top two bytes are its last two.
        out = bytearray(len(samples) // width * 2)
        out[0::2] = samples[width - 2 :: width]
        out[1::2] = samples[width - 1 :: width]
        pcm = bytes(out)
    elif audio_format == _FLOAT and bits == 32:
        floats = array("f", samples)
        if sys.byteorder == "big":
            floats.byteswap()
        pcm = _little_endian(array("h", (int(max(-1.0, min(1.0, x)) * 32767) for x in floats)))
    else:
        raise ValueError(f"unsupported WAV encoding (format {audio_format}, {bits}-bit)")
    return rate, channels, pcm


def emit(event: dict) -> None:
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


class Player:
    def __init__(self) -> None:
        self.queue: deque[tuple[int, bytes]] = deque()
        self.lock = threading.Condition()
        self.paused = False
        self.stopping = False
        self.quitting = False

    def command(self, msg: dict) -> None:
        with self.lock:
            cmd = msg.get("cmd")
            if cmd == "play":
                self.queue.append((int(msg.get("id", 0)), base64.b64decode(msg["wav"])))
            elif cmd == "pause":
                self.paused = True
            elif cmd == "resume":
                self.paused = False
            elif cmd == "stop":
                self.queue.clear()
                self.stopping = True
                self.paused = False
            elif cmd == "quit":
                self.quitting = True
                self.queue.clear()
                self.stopping = True
            self.lock.notify_all()

    def run(self) -> None:
        import sounddevice

        was_busy = False
        while True:
            with self.lock:
                while not self.queue and not self.quitting:
                    if was_busy:
                        emit({"event": "idle"})
                        was_busy = False
                    self.lock.wait()
                if self.quitting:
                    return
                clip_id, data = self.queue.popleft()
                self.stopping = False
            was_busy = True
            try:
                self.play(sounddevice, clip_id, data)
            except Exception as exc:  # noqa: BLE001 — report and carry on with the next clip
                emit({"event": "error", "message": str(exc)})

    def play(self, sounddevice, clip_id: int, data: bytes) -> None:
        rate, channels, pcm = to_pcm16(data)
        frame_bytes = channels * 2
        total = len(pcm) // frame_bytes
        block = max(1, int(rate * _BLOCK_SECONDS))
        emit({"event": "start", "id": clip_id})
        stream = sounddevice.RawOutputStream(samplerate=rate, channels=channels, dtype="int16")
        stream.start()
        try:
            at, last_report = 0, 0.0
            while at < total:
                with self.lock:
                    while self.paused and not self.stopping:
                        self.lock.wait()
                    if self.stopping:
                        return
                end = min(total, at + block)
                stream.write(pcm[at * frame_bytes : end * frame_bytes])
                at = end
                now = time.monotonic()
                if now - last_report >= _PROGRESS_EVERY:
                    emit({"event": "progress", "id": clip_id, "fraction": round(at / total, 3)})
                    last_report = now
            emit({"event": "done", "id": clip_id})
        finally:
            stream.stop()
            stream.close()


def main() -> None:
    player = Player()
    threading.Thread(target=player.run, daemon=True).start()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            player.command(json.loads(line))
        except (ValueError, KeyError) as exc:
            emit({"event": "error", "message": f"bad command: {exc}"})
    player.command({"cmd": "quit"})


if __name__ == "__main__":
    main()
