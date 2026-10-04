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
import io
import json
import sys
import threading
import time
import wave
from collections import deque

_BLOCK_SECONDS = 0.05
_PROGRESS_EVERY = 0.15


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
        with wave.open(io.BytesIO(data), "rb") as wav:
            rate, channels, width = wav.getframerate(), wav.getnchannels(), wav.getsampwidth()
            pcm = wav.readframes(wav.getnframes())
        if width != 2:
            raise ValueError(f"only 16-bit WAV is supported (got {width * 8}-bit)")
        frame_bytes = channels * width
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
