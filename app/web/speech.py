"""Speaking a narration or reply aloud (try_speak). Shared by the narration,
explore and voice handlers, so it lives below all of them."""

from __future__ import annotations

import asyncio
import base64
import logging

from fastapi import WebSocket

from ..services.voice_service import VoiceServiceError
from ..utils.speech_text import humanize_for_speech, sentence_segments, split_for_speech
from . import runtime
from .runtime import TTS_MAX_CHARS, TTS_MAX_WORDS, TTS_MIME_TYPE, send_json
from .session import Session

log = logging.getLogger("ai_pear_review")


async def try_speak(
    ws: WebSocket,
    session: Session,
    text: str,
    msg_type: str = "audio_chunk",
    check_tts_pref: bool = True,
) -> None:
    """Speaks `text` aloud, and tells the client TTS is down if it fails.

    The text itself has already been sent separately, so a failure here
    costs the audio, never the content.

    Sends "audio_chunk": {"audio_base64", "mime_type", "chunk_index",
    "chunk_count", "sentences"}. Text over the configured budget (tts.max_chars and
    tts.max_words in config.yaml — see speech_text.split_for_speech) is
    split at sentence or word boundaries and sent as consecutive clips the
    frontend plays back to back; nothing is truncated. chunk_index and
    chunk_count are always present, 0 and 1 for the common single-clip
    case, because the frontend keys "new turn, reset the queue" off
    chunk_index == 0. The same budget governs handle_speak_file, so both
    TTS paths obey one setting.

    What gets spoken is a humanized version of `text`, with snake_case and
    camelCase identifiers turned into words (see speech_text.py). Narration
    and reply callers pass the sanitised, spoken half of
    sanitize_persona_reply rather than the raw response — the transcript
    already has the full original text and the same sanitised blocks, so
    nothing here is the only copy of anything.

    msg_type and check_tts_pref exist for handle_speak_text in
    app/handlers/voice.py, the guided tour's read-aloud, which reuses this
    synthesis, chunking and failure handling rather than duplicating it. It
    differs twice over. It sends "tour_audio_chunk", because tour audio
    plays through its own queue and must not fight narration's over the
    shared <audio> element or the transcript's replay button. And it passes
    check_tts_pref=False, because the tour has its own on/off toggle,
    already applied client-side — gating it again on the narration voice
    preference would make that toggle look broken to anyone who wants one
    and not the other."""
    if check_tts_pref and not session.tts_enabled:
        # Reviewer turned voice output off (see "set_voice_prefs") — skip
        # the real TTS HTTP call entirely rather than synthesize audio the
        # client would just discard.
        return

    # Split first, humanize each piece second — the same order
    # handle_speak_file uses, and the order the configured budget is
    # defined against (see speech_text.py's module docstring). The two are
    # equivalent for every cut this splitter makes anyway: neither
    # identifier pattern in speech_text can match across whitespace, and
    # every cut is at whitespace.
    pieces = split_for_speech(text, TTS_MAX_CHARS, TTS_MAX_WORDS)
    if not pieces:
        return

    for i, piece in enumerate(pieces):
        try:
            audio_bytes = await asyncio.to_thread(runtime.TTS.synthesize, humanize_for_speech(piece))
        except VoiceServiceError as exc:
            # Abort the rest of the sequence rather than skipping one clip
            # and carrying on — a gap mid-sentence with no signal is the
            # "partial success" outcome handle_speak_file also rejects.
            #
            # But stay silent about it (no send_error), unlike
            # handle_speak_file: this text was already delivered to the
            # transcript, so the reviewer can simply read it, and the
            # status pill flipping is proportionate. A file read has no
            # text — audio is the whole product there, so silence would
            # look like a hang. The two paths differ on purpose.
            log.info("TTS unavailable: %s", exc)
            await send_json(ws, "service_status", {"tts": False})
            return
        if i == 0:
            await send_json(ws, "service_status", {"tts": True})
        await send_json(
            ws,
            msg_type,
            {
                "audio_base64": base64.b64encode(audio_bytes).decode("ascii"),
                "mime_type": TTS_MIME_TYPE,
                # Always present, even for a single clip: the frontend uses
                # chunk_index === 0 as its "new turn, reset the queue"
                # trigger, which an optional field would make ambiguous.
                "chunk_index": i,
                "chunk_count": len(pieces),
                # For the read-along highlight: which sentences this clip
                # says, and how its length divides between them. The tour's
                # player ignores it.
                "sentences": sentence_segments(piece),
            },
        )
