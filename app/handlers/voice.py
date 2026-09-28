"""Voice preferences, the guided tour's read-aloud, and markdown files:
preview and read-aloud."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging

from fastapi import WebSocket

from ..services.editor_service import read_current_file
from ..services.voice_service import VoiceServiceError
from ..utils.markdown_speech import (
    block_to_payload,
    chunk_block_to_payload,
    chunk_blocks,
    parse_markdown_blocks,
)
from ..utils.speech_text import humanize_for_speech
from ..web import runtime
from ..web.config import CONFIG
from ..web.runtime import TTS_MAX_CHARS, TTS_MAX_WORDS, TTS_MIME_TYPE, send_error, send_json
from ..web.session import Session
from ..web.speech import try_speak
from .registry import handler

log = logging.getLogger("ai_pear_review")


@handler("set_voice_prefs")
async def handle_set_voice_prefs(ws: WebSocket, session: Session, payload: dict) -> None:
    """Records the reviewer's speech-to-text and text-to-speech preferences
    on the session.

    Payload: {"stt_enabled": bool, "tts_enabled": bool}, either key
    optional. Holding this server-side lets the real TTS HTTP call be
    skipped entirely when output is off, rather than relying on the browser
    staying silent after synthesis has already happened.

    The client sends it once when the socket opens, from its last saved
    preference, and again on every change. No reply: this updates a
    preference, it isn't an action with a visible result."""
    if isinstance(payload.get("stt_enabled"), bool):
        session.stt_enabled = payload["stt_enabled"]
    if isinstance(payload.get("tts_enabled"), bool):
        session.tts_enabled = payload["tts_enabled"]


def _content_hash(content: str) -> str:
    """Identifies which revision of a file a preview and a read agree on.

    Files really can change underneath both — confirm_act_now writes to
    disk — and a stale block index would highlight the wrong paragraph
    rather than fail loudly. On a mismatch the frontend drops its highlight
    updates and still plays the audio.

    Short on purpose: this distinguishes revisions, it doesn't authenticate
    anything."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


async def _read_md_file(ws: WebSocket, payload: dict) -> tuple[str, str] | None:
    """Reads the markdown file named in a payload, for both entry points
    below.

    Returns (file_path, content), or None once it has sent the error itself.

    The .md check is defence in depth: the frontend only offers these
    actions on .md pills, but nothing stops a hand-crafted message asking
    for something else. Keeping the path inside the repo is a separate
    guard, in editor_service.resolve_within_repo."""
    file_path = payload.get("file_path")
    if not isinstance(file_path, str) or not file_path.lower().endswith(".md"):
        await send_error(ws, "Markdown actions are only available for .md files.")
        return None
    repo_path = CONFIG["server"].get("repo_path", ".")
    content = await asyncio.to_thread(read_current_file, repo_path, file_path)
    if content is None:
        await send_error(ws, f"Could not read {file_path}.")
        return None
    return file_path, content


@handler("open_md_preview")
async def handle_open_md_preview(ws: WebSocket, session: Session, payload: dict) -> None:
    """Parses one markdown file into render-ready blocks for the preview
    pane.

    Payload: {"file_path": str}, sent when the reviewer clicks the preview
    icon on a .md pill. Replies "md_preview" ({"file_path", "content_hash",
    "blocks", "text"}), where each block is a heading, paragraph, list_item, quote,
    code, table_row or rule carrying the source line range it came from and
    a list of inline spans ({"text", "style", "href"}).

    Spans exist so the frontend can build real DOM nodes with .textContent
    and never parse or inject markup — file content never becomes HTML
    anywhere in this app.

    Does not cancel whatever else is running: opening a preview must not
    stop audio the reviewer is already listening to, possibly of a different
    file. Awaited inline rather than wrapped in a task for the same reason
    step_into is — one file read, one reply, nothing worth cancelling."""
    result = await _read_md_file(ws, payload)
    if result is None:
        return
    file_path, content = result
    blocks = parse_markdown_blocks(content)
    if not blocks:
        await send_error(ws, f"{file_path} has no content to preview.")
        return
    await send_json(
        ws,
        "md_preview",
        {
            "file_path": file_path,
            "content_hash": _content_hash(content),
            "blocks": [block_to_payload(b) for b in blocks],
            # The raw markdown, for the preview's Copy plan button.
            "text": content,
        },
    )


def _clamp_line_range(payload: dict, blocks: list) -> tuple[int, int] | tuple[None, None]:
    """The optional start_line/end_line on "speak_file". Absent or
    malformed means "read the whole file" rather than an error — the same
    degrade-rather-than-refuse posture the rest of this file takes, and the
    frontend always sends a well-formed pair anyway."""
    start = payload.get("start_line")
    end = payload.get("end_line")
    if start is None and end is None:
        return None, None

    def valid(value: object) -> bool:
        return isinstance(value, int) and not isinstance(value, bool)

    if not valid(start) or not valid(end):
        return None, None
    if start > end:
        start, end = end, start
    last = max(b.end_line for b in blocks)
    return max(1, start), min(last, end)


@handler("speak_file", cancels=True, background=True)
async def handle_speak_file(ws: WebSocket, session: Session, payload: dict) -> None:
    """Reads a markdown file aloud, streaming the audio back in chunks.

    Payload: {"file_path": str}, plus optional {"start_line", "end_line"} to
    read only the reviewer's block selection in the preview. Sent from the
    speaker icon on a .md pill in the sidebar, or a read button in the
    preview, and only offered for .md files already in the current diff.

    The file is parsed into line-anchored blocks and packed into TTS-sized
    chunks (utils/markdown_speech.py — the real TTS endpoint is a
    single-shot call with no confirmed max length), then streamed back as
    "file_audio_chunk" messages bracketed by start/done "notice"s. A line
    range selects by overlap, so a block straddling the boundary is read in
    full.

    Cancellable via "stop" like every other long-running action: every
    iteration of the loop below sits on an await, each one a cancellation
    point, and there is no partial write to clean up on the way out — so
    nothing beyond the `finally` clearing session.tts_reading_file is
    needed."""
    result = await _read_md_file(ws, payload)
    if result is None:
        return
    file_path, content = result

    blocks = parse_markdown_blocks(content)
    start_line, end_line = _clamp_line_range(payload, blocks) if blocks else (None, None)
    include = None
    if start_line is not None:
        # Overlap, not containment: a block straddling the boundary is read
        # in full. The frontend always sends exact block boundaries so the
        # two are the same in practice, and reading a whole block is the
        # right call for a block-granular feature anyway.
        #
        # Passed as an index filter rather than by slicing `blocks`: the
        # block_index that goes back to the browser has to index the whole
        # document, which is what the open preview is keyed on. Slicing
        # would renumber from zero and highlight the wrong paragraphs.
        include = {i for i, b in enumerate(blocks) if b.end_line >= start_line and b.start_line <= end_line}

    chunks = chunk_blocks(blocks, TTS_MAX_CHARS, include, max_words=TTS_MAX_WORDS)
    if not chunks:
        where = "the selected part of " if start_line is not None else ""
        await send_error(ws, f"Nothing readable in {where}{file_path}.")
        return

    if not session.tts_enabled:
        await send_error(ws, "Turn on Speech to read a file aloud.")
        return

    content_hash = _content_hash(content)
    session.tts_reading_file = file_path
    try:
        await send_json(ws, "notice", {"level": "info", "message": f"Reading {file_path}..."})
        for i, chunk in enumerate(chunks):
            try:
                audio_bytes = await asyncio.to_thread(runtime.TTS.synthesize, humanize_for_speech(chunk.text))
            except VoiceServiceError as exc:
                # Abort the whole read rather than skip this chunk and
                # continue — matches try_speak's existing "unavailable this
                # turn" precedent instead of inventing a new
                # partial-success semantics that would leave the reviewer
                # unsure whether anything was silently dropped.
                log.info("TTS unavailable while reading %s: %s", file_path, exc)
                await send_json(ws, "service_status", {"tts": False})
                await send_error(ws, f"Voice output stopped partway through {file_path} (TTS unavailable).")
                return
            await send_json(ws, "service_status", {"tts": True})
            await send_json(
                ws,
                "file_audio_chunk",
                {
                    "file_path": file_path,
                    "chunk_index": i,
                    "chunk_count": len(chunks),
                    "audio_base64": base64.b64encode(audio_bytes).decode("ascii"),
                    "mime_type": TTS_MIME_TYPE,
                    # Highlight metadata. A client with no preview open just
                    # ignores these, which is what keeps the plain
                    # speaker-icon path behaving exactly as it did before
                    # the preview existed. Deliberately no chunk text: the
                    # client already has every block's spans from
                    # "md_preview", and a second copy could disagree.
                    "content_hash": content_hash,
                    "start_line": chunk.start_line,
                    "end_line": chunk.end_line,
                    "blocks": [chunk_block_to_payload(m) for m in chunk.blocks],
                },
            )
        await send_json(ws, "notice", {"level": "success", "message": f"Finished reading {file_path}."})
    finally:
        session.tts_reading_file = None


@handler("speak_turn", cancels=True, background=True)
async def handle_speak_turn(ws: WebSocket, session: Session, payload: dict) -> None:
    """Reads one chat turn aloud on request — the speaker button on a
    presenter turn.

    Payload: {"text": str}, the turn's own "spoken" field, so what's read is
    exactly what automatic narration would have read. check_tts_pref=False
    is the point of it: with voice output off, nothing is spoken as the
    review moves along, and the reviewer picks which replies to hear.
    Sends "turn_audio_chunk" rather than "audio_chunk", because the clips
    belong to the turn that was clicked, not to the latest one."""
    await try_speak(
        ws,
        session,
        payload.get("text") or "",
        msg_type="turn_audio_chunk",
        check_tts_pref=False,
    )


@handler("speak_text", cancels=True, background=True)
async def handle_speak_text(ws: WebSocket, session: Session, payload: dict) -> None:
    """Speaks a line of the guided tour aloud.

    Payload: {"text": str}. See try_speak's docstring for why this passes
    msg_type and check_tts_pref rather than calling it the way narration and
    replies do.

    The text is fixed onboarding copy, nothing to do with the diff, so
    unlike most handlers this never reads session.index or
    session.current_hunk."""
    await try_speak(
        ws,
        session,
        payload.get("text") or "",
        msg_type="tour_audio_chunk",
        check_tts_pref=False,
    )
