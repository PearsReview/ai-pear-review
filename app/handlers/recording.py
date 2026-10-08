"""Server-side push-to-talk, for clients that can't use the microphone
themselves (the VS Code extension — see services/recorder.py). The browser
UI never sends these.

The recording comes back to the client rather than going straight to STT:
the client then sends it as `audio_base64` on whichever message it was for
(`reply`, `act_now`, `request_change`, `explore_reply`), exactly as the
browser does with its own recording. So what a voice turn means stays
decided in one place, the client, as it is today."""

from __future__ import annotations

import asyncio
import base64

from fastapi import WebSocket

from ..services.recorder import RecorderError, wav_duration_seconds
from ..web.runtime import send_error, send_json
from ..web.session import Session
from .registry import handler

# Shorter than this is a mis-press, not speech. The browser path says the
# same thing for the same reason ("Recording too short", mic.js).
_MIN_SECONDS = 0.3


@handler("start_recording")
async def handle_start_recording(ws: WebSocket, session: Session, payload: dict) -> None:
    """Opens the microphone. Payload: none. Replies "recording_state"
    {"recording": true}, or an error if recording isn't available here."""
    try:
        await asyncio.to_thread(session.recorder.start)
    except RecorderError as exc:
        await send_error(ws, str(exc))
        return
    await send_json(ws, "recording_state", {"recording": True})


@handler("stop_recording")
async def handle_stop_recording(ws: WebSocket, session: Session, payload: dict) -> None:
    """Closes the microphone and sends the audio back. Payload: none.

    Replies "recording_state" {"recording": false}, then "recording_result"
    {"audio_base64", "mime_type", "duration_seconds"} — or an error when
    nothing was recording or the clip is too short to be speech."""
    try:
        wav = await asyncio.to_thread(session.recorder.stop)
    except RecorderError as exc:
        await send_error(ws, str(exc))
        return
    await send_json(ws, "recording_state", {"recording": False})
    duration = wav_duration_seconds(wav)
    if duration < _MIN_SECONDS:
        await send_error(ws, "Recording too short — keep recording a little longer.")
        return
    await send_json(
        ws,
        "recording_result",
        {
            "audio_base64": base64.b64encode(wav).decode("ascii"),
            "mime_type": "audio/wav",
            "duration_seconds": round(duration, 2),
        },
    )
