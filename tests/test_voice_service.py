"""STTClient/TTSClient's optional bearer-token auth header - added
alongside the settings-panel token fields (see settings_store.py's
ALLOWED_TTS_KEYS/ALLOWED_STT_KEYS). The self-hosted service this app is
tested against needs no auth at all, so the header must be entirely
absent when no token is configured - not an empty/None Authorization
value, which some servers treat differently from "no header sent"."""

from __future__ import annotations

import requests

from app.services.voice_service import STTClient, TTSClient


class _FakeResponse:
    def __init__(self, *, json_body=None, content=b""):
        self._json_body = json_body if json_body is not None else {}
        self.content = content

    def raise_for_status(self):
        pass

    def json(self):
        return self._json_body


def _capture_request(monkeypatch, response: _FakeResponse):
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append({"method": method, "url": url, **kwargs})
        return response

    monkeypatch.setattr(requests, "request", fake_request)
    return calls


def test_stt_sends_no_authorization_header_when_no_token_configured(monkeypatch):
    calls = _capture_request(monkeypatch, _FakeResponse(json_body={"text": "hello"}))
    client = STTClient({"endpoint": "http://x/transcribe"})

    client.transcribe(b"audio-bytes")

    assert "Authorization" not in calls[0]["headers"]


def test_stt_sends_bearer_token_when_configured_multipart(monkeypatch):
    calls = _capture_request(monkeypatch, _FakeResponse(json_body={"text": "hello"}))
    client = STTClient({"endpoint": "http://x/transcribe", "token": "s3cr3t"})

    client.transcribe(b"audio-bytes")

    assert calls[0]["headers"]["Authorization"] == "Bearer s3cr3t"


def test_stt_sends_bearer_token_when_configured_non_multipart(monkeypatch):
    """The non-multipart branch also sets its own Content-Type header -
    the token header must be added alongside it, not replace it."""
    calls = _capture_request(monkeypatch, _FakeResponse(json_body={"text": "hello"}))
    client = STTClient({"endpoint": "http://x/transcribe", "request_format": "raw", "token": "s3cr3t"})

    client.transcribe(b"audio-bytes")

    assert calls[0]["headers"]["Authorization"] == "Bearer s3cr3t"
    assert calls[0]["headers"]["Content-Type"] == "application/octet-stream"


def test_tts_sends_no_authorization_header_when_no_token_configured(monkeypatch):
    calls = _capture_request(monkeypatch, _FakeResponse(content=b"wav-bytes"))
    client = TTSClient({"endpoint": "http://x/speech"})

    client.synthesize("hello")

    assert not calls[0]["headers"]


def test_tts_sends_bearer_token_when_configured_json(monkeypatch):
    calls = _capture_request(monkeypatch, _FakeResponse(content=b"wav-bytes"))
    client = TTSClient({"endpoint": "http://x/speech", "token": "s3cr3t"})

    client.synthesize("hello")

    assert calls[0]["headers"]["Authorization"] == "Bearer s3cr3t"


def test_tts_sends_bearer_token_when_configured_non_json(monkeypatch):
    calls = _capture_request(monkeypatch, _FakeResponse(content=b"wav-bytes"))
    client = TTSClient({"endpoint": "http://x/speech", "request_format": "raw", "token": "s3cr3t"})

    client.synthesize("hello")

    assert calls[0]["headers"]["Authorization"] == "Bearer s3cr3t"
