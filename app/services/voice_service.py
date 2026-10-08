"""STT/TTS adapters for a speech service — self-hosted by default, but any
HTTP endpoint matching the contract below, local or remote (see `token`).

The request/response contract is confirmed against a real service
(faster-whisper STT + kokoro-onnx TTS, both served from one FastAPI app): STT is a multipart upload with the
audio in a field named "file", returning {"text": ...}; TTS is a JSON
{"text": ...} POST returning raw audio/wav bytes. Everything downstream
talks to the STTClient/TTSClient interface below, so the actual
multipart/JSON shape can still be adjusted here in one place if the service
changes.

A failed call raises VoiceServiceError; callers treat that as "unavailable
for this turn" rather than crashing.
"""

from __future__ import annotations

import requests

from .errors import ServiceError


class VoiceServiceError(ServiceError):
    """Raised when an STT/TTS call fails (endpoint down, bad response, etc.)."""


# faster-whisper (and Whisper generally) is well known to hallucinate a
# small set of canned phrases on silent/near-silent audio — trained partly
# on captioned video, it defaults to things like "Thanks for watching!"
# rather than an honest empty string. Treating a match as real speech would
# mean the conversation agent responds to words the reviewer never said.
# There's no "better prompt" fix for this — silence has no content to
# extract — so this is a denylist, not a model/prompting problem. A more
# robust fix belongs server-side (filtering on faster-whisper's own
# per-segment no_speech_prob confidence, in the STT server itself, not this
# repo) — this is the practical guard on this side of the HTTP boundary.
_HALLUCINATION_PHRASES = {
    "thanks for watching",
    "thank you for watching",
    "please subscribe",
    "like and subscribe",
    "see you next time",
    "bye",
}


def _looks_like_hallucination(text: str) -> bool:
    return text.strip().lower().rstrip(".!") in _HALLUCINATION_PHRASES


def _audio_upload(audio_bytes: bytes) -> tuple[str, bytes, str]:
    """(filename, bytes, mime type) for the multipart upload, labelled with
    what the audio really is. Browsers' MediaRecorder produces WebM/Opus
    (or Ogg on Firefox), never WAV; the server-side recorder
    (recorder.py) produces WAV."""
    if audio_bytes[:4] == b"RIFF" and audio_bytes[8:12] == b"WAVE":
        return ("audio.wav", audio_bytes, "audio/wav")
    return ("audio.webm", audio_bytes, "audio/webm")


class _SpeechClient:
    """Shared endpoint and auth wiring for the STT and TTS adapters.

    Auth is optional — the self-hosted service this app is tested against
    needs none. Set a token only for a hosted/remote endpoint that requires
    one. It is never logged or echoed to the client (see
    settings_store.effective_{stt,tts}_settings, which report only whether
    one is set)."""

    def __init__(self, config: dict) -> None:
        self.endpoint = config["endpoint"]
        self.method = config.get("method", "POST")
        self.token = config.get("token")

    def _auth_headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}


class STTClient(_SpeechClient):
    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.request_format = config.get("request_format", "multipart")
        self.response_field = config.get("response_field", "text")
        self.timeout = config.get("timeout_seconds", 15)

    def transcribe(self, audio_bytes: bytes) -> str:
        """Sends raw recorded audio to the configured STT endpoint and
        returns the transcript, or "" if the result looks like a known
        Whisper silence-hallucination (see _looks_like_hallucination) —
        never raises for that case, only for a real call failure."""
        try:
            if self.request_format == "multipart":
                resp = requests.request(
                    self.method,
                    self.endpoint,
                    files={"file": _audio_upload(audio_bytes)},
                    headers=self._auth_headers(),
                    timeout=self.timeout,
                )
            else:
                resp = requests.request(
                    self.method,
                    self.endpoint,
                    data=audio_bytes,
                    headers={"Content-Type": "application/octet-stream", **self._auth_headers()},
                    timeout=self.timeout,
                )
            resp.raise_for_status()
            data = resp.json()
            text = data[self.response_field]
        except (requests.RequestException, ValueError, KeyError) as exc:
            raise VoiceServiceError(f"STT call failed: {exc}") from exc

        return "" if _looks_like_hallucination(text) else text


class TTSClient(_SpeechClient):
    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.request_format = config.get("request_format", "json")
        self.timeout = config.get("timeout_seconds", 30)

    def synthesize(self, text: str) -> bytes:
        """Sends text to the configured TTS endpoint and returns raw audio
        bytes in whatever format that endpoint produces (config.yaml's
        tts.mime_type names it for the caller — this method doesn't
        inspect or validate the response's actual content type)."""
        try:
            if self.request_format == "json":
                resp = requests.request(
                    self.method,
                    self.endpoint,
                    json={"text": text},
                    headers=self._auth_headers(),
                    timeout=self.timeout,
                )
            else:
                resp = requests.request(
                    self.method,
                    self.endpoint,
                    data=text.encode("utf-8"),
                    headers=self._auth_headers(),
                    timeout=self.timeout,
                )
            resp.raise_for_status()
            return resp.content
        except requests.RequestException as exc:
            raise VoiceServiceError(f"TTS call failed: {exc}") from exc
