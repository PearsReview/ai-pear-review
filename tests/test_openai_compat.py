"""The OpenAI-compatible path — OpenAICompatProvider construction, the
adapter's streamed single-call plumbing (complete), list_models, and the
cancellation watcher.

Mirrors tests/test_conversation_service_anthropic.py: the provider had no
dedicated coverage, only the generic registry checks in test_providers.py.
These are mocked unit tests (no endpoint or key in this environment), but
they pin the request/response shape the adapter builds and parses, the
misconfiguration error messages a user will actually see, and that an
abandoned call hangs up instead of generating to the end.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from openai import APIError

from app.providers import OpenAICompatProvider
from app.providers.base import Capability, ConversationCancelled, ConversationError

REPO_ROOT = Path(__file__).resolve().parent.parent


class _Delta:
    def __init__(self, content: str | None) -> None:
        self.content = content


class _Choice:
    def __init__(self, content: str | None = None, finish_reason: str | None = None) -> None:
        self.delta = _Delta(content)
        self.finish_reason = finish_reason


class _Usage:
    def __init__(self, prompt_tokens: int, completion_tokens: int) -> None:
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


class _Chunk:
    def __init__(self, choices: list | None = None, usage: _Usage | None = None) -> None:
        self.choices = choices or []
        self.usage = usage


def _reply(text: str, in_tokens: int = 10, out_tokens: int = 5, finish_reason: str = "stop") -> list[_Chunk]:
    """A normal streamed reply: a content chunk, then the usage-only final
    chunk (include_usage) whose choices are empty."""
    return [
        _Chunk([_Choice(content=text, finish_reason=finish_reason)]),
        _Chunk([], usage=_Usage(in_tokens, out_tokens)),
    ]


class _FakeStream:
    """Stands in for the SDK's streaming response: chunks arrive while
    iterating, and close() from another thread makes a blocked read raise,
    the way the real HTTP library surfaces a mid-stream close."""

    def __init__(self, chunks: list, silent_after: int | None = None) -> None:
        self._chunks = chunks
        self._silent_after = silent_after
        self._closed = threading.Event()
        self.closed = False

    def __iter__(self):
        for i, chunk in enumerate(self._chunks):
            if self._silent_after is not None and i == self._silent_after:
                if not self._closed.wait(10):
                    raise AssertionError("the silent stream was never closed")
                raise RuntimeError("read on a closed stream")  # the HTTP library's error, in the real SDK
            yield chunk

    def close(self) -> None:
        self.closed = True
        self._closed.set()


class _FakeCompletions:
    """Records the create() call and hands back the queued stream, or raises
    the queued error — the "spy plus canned response" shape the anthropic and
    voice-service fakes use."""

    def __init__(
        self, chunks: list | None = None, error: Exception | None = None, silent_after: int | None = None
    ) -> None:
        self._chunks = chunks or []
        self._error = error
        self._silent_after = silent_after
        self.calls: list[dict] = []
        self.streams: list[_FakeStream] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        stream = _FakeStream(self._chunks, self._silent_after)
        self.streams.append(stream)
        return stream


def _provider_with_fake_completions(completions: _FakeCompletions) -> OpenAICompatProvider:
    """__new__-bypass so no API key or real SDK client is needed — sets
    exactly what complete() touches."""
    provider = OpenAICompatProvider.__new__(OpenAICompatProvider)
    provider.model = "gpt-5"
    provider.num_ctx = None
    provider.base_url = None
    chat = type("Chat", (), {"completions": completions})()
    provider._client = type("FakeOpenAIClient", (), {"chat": chat})()
    return provider


def _complete(provider: OpenAICompatProvider, messages: list[dict], system: str, max_tokens: int = 300):
    result = provider.complete(messages, system, max_tokens, None, None)
    return result.text, result.input_tokens, result.output_tokens


# --- construction (the misconfiguration messages a user will see) ---


def test_missing_model_raises_a_clear_error():
    with pytest.raises(ConversationError, match=r"conversation\.openai\.model is not set"):
        OpenAICompatProvider({"base_url": "http://localhost/v1"}, 30.0)


def test_missing_api_key_raises_a_clear_error(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ConversationError, match="OPENAI_API_KEY is not set"):
        OpenAICompatProvider({"model": "gpt-5"}, 30.0)


def test_missing_api_key_error_names_a_custom_env_var(monkeypatch):
    monkeypatch.delenv("MY_KEY", raising=False)
    with pytest.raises(ConversationError, match="MY_KEY is not set"):
        OpenAICompatProvider({"model": "gpt-5", "api_key_env": "MY_KEY"}, 30.0)


def test_providers_import_without_the_openai_package():
    """A venv made before openai joined requirements.txt (the VS Code
    extension's managed one) must still start the backend for an Ollama or
    Anthropic user — importing app.providers can't need openai."""
    code = "import sys; sys.modules['openai'] = None; import app.providers; print('ok')"
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=REPO_ROOT)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_missing_openai_package_raises_a_clear_error(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr("app.providers.openai_compat.openai", None)
    with pytest.raises(ConversationError, match="openai package is not installed"):
        OpenAICompatProvider({"model": "gpt-5"}, 30.0)
    with pytest.raises(ConversationError, match="openai package is not installed"):
        OpenAICompatProvider.list_models({})
    assert OpenAICompatProvider.available({}) is False


def test_constructs_successfully_with_a_key_present(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(
        "app.providers.openai_compat.openai.OpenAI",
        lambda api_key, base_url, timeout: object(),
    )
    provider = OpenAICompatProvider({"model": "gpt-5", "base_url": "http://localhost/v1"}, 30.0)
    assert provider.model == "gpt-5"
    assert provider.base_url == "http://localhost/v1"
    assert provider.prompt_tier() == "frontier"


def test_blank_base_url_becomes_none(monkeypatch):
    """A blank base_url must reach the SDK as None (its default), not "" —
    the adapter normalises it so an unset field doesn't point the client at
    an empty host."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    captured: dict = {}
    monkeypatch.setattr(
        "app.providers.openai_compat.openai.OpenAI",
        lambda api_key, base_url, timeout: captured.update(base_url=base_url) or object(),
    )
    OpenAICompatProvider({"model": "gpt-5", "base_url": "   "}, 30.0)
    assert captured["base_url"] is None


# --- available ---


def test_available_follows_the_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert OpenAICompatProvider.available({}) is True
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert OpenAICompatProvider.available({}) is False


# --- list_models (settings panel's model choices) ---


def test_list_models_returns_sorted_ids(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    listed = [type("M", (), {"id": "gpt-5"})(), type("M", (), {"id": "claude-sonnet"})()]
    monkeypatch.setattr(
        "app.providers.openai_compat.openai.OpenAI",
        lambda api_key, base_url, timeout: type(
            "C", (), {"models": type("Models", (), {"list": lambda self: listed})()}
        )(),
    )
    # Unlike the Anthropic adapter (which preserves API order), this one sorts.
    assert OpenAICompatProvider.list_models({}) == ["claude-sonnet", "gpt-5"]


def test_list_models_is_empty_without_a_key_rather_than_raising(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert OpenAICompatProvider.list_models({}) == []


# --- complete: request shape and reply parsing ---


def test_complete_builds_the_right_request_and_parses_the_reply():
    completions = _FakeCompletions(_reply("Looks like a guard clause.", in_tokens=120, out_tokens=18))
    provider = _provider_with_fake_completions(completions)

    text, in_tokens, out_tokens = _complete(
        provider, [{"role": "user", "content": "What changed here?"}], "You are a reviewer."
    )

    assert text == "Looks like a guard clause."
    assert (in_tokens, out_tokens) == (120, 18)
    assert len(completions.calls) == 1
    call = completions.calls[0]
    assert call["model"] == "gpt-5"
    # OpenAI has no top-level system field; it is the first message.
    assert call["messages"][0] == {"role": "system", "content": "You are a reviewer."}
    assert call["messages"][1] == {"role": "user", "content": "What changed here?"}
    assert call["max_tokens"] == 300
    assert call["stream"] is True


def test_complete_joins_content_across_chunks():
    chunks = [
        _Chunk([_Choice(content="Part one. ")]),
        _Chunk([_Choice(content=None)]),  # a chunk with no content (e.g. a role delta) is skipped
        _Chunk([_Choice(content="Part two.", finish_reason="stop")]),
        _Chunk([], usage=_Usage(5, 5)),
    ]
    provider = _provider_with_fake_completions(_FakeCompletions(chunks))

    text, _, _ = _complete(provider, [{"role": "user", "content": "hi"}], "sys")

    assert text == "Part one. Part two."


def test_complete_defaults_tokens_to_zero_when_no_usage_chunk_arrives():
    provider = _provider_with_fake_completions(
        _FakeCompletions([_Chunk([_Choice(content="ok", finish_reason="stop")])])
    )

    _, in_tokens, out_tokens = _complete(provider, [{"role": "user", "content": "hi"}], "sys")

    assert (in_tokens, out_tokens) == (0, 0)


# --- the reply-token cap ---


def test_a_reply_cut_off_at_the_cap_is_still_returned():
    completions = _FakeCompletions(_reply("It works because the sandbox", finish_reason="length"))
    provider = _provider_with_fake_completions(completions)
    text, _, _ = _complete(provider, [{"role": "user", "content": "why?"}], "sys")
    assert text == "It works because the sandbox"


def test_reasoning_eating_the_whole_cap_names_the_setting_to_raise():
    chunks = [_Chunk([_Choice(content="", finish_reason="length")]), _Chunk([], usage=_Usage(10, 300))]
    provider = _provider_with_fake_completions(_FakeCompletions(chunks))
    with pytest.raises(ConversationError, match=r"conversation\.openai\.max_tokens"):
        _complete(provider, [{"role": "user", "content": "why?"}], "sys")


# --- errors ---


def test_complete_wraps_api_error_as_conversation_error():
    api_error = APIError("rate limited", request=object(), body=None)
    provider = _provider_with_fake_completions(_FakeCompletions(error=api_error))
    with pytest.raises(ConversationError, match="OpenAI-compatible API call failed"):
        _complete(provider, [{"role": "user", "content": "hi"}], "sys")


# --- Stop: an abandoned call hangs up instead of generating to the end ---


def _complete_with_cancel(provider: OpenAICompatProvider, cancel: threading.Event):
    return provider.complete([{"role": "user", "content": "hi"}], "sys", 300, None, cancel)


def test_the_provider_declares_cancellation_and_model_listing():
    assert OpenAICompatProvider.capabilities & Capability.CANCELLATION
    assert OpenAICompatProvider.capabilities & Capability.MODEL_LISTING


def test_stop_hangs_up_a_silent_stream_promptly():
    chunks = [_Chunk([_Choice(content="never")]) for _ in range(5)]
    completions = _FakeCompletions(chunks, silent_after=1)
    provider = _provider_with_fake_completions(completions)
    cancel = threading.Event()
    threading.Timer(0.3, cancel.set).start()
    started = time.monotonic()

    with pytest.raises(ConversationCancelled):
        _complete_with_cancel(provider, cancel)
    assert time.monotonic() - started < 2
    assert completions.streams[0].closed


def test_stop_while_chunks_are_flowing_is_noticed_between_them():
    chunks = [_Chunk([_Choice(content="x")]) for _ in range(50)]
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(ConversationCancelled):
        _complete_with_cancel(_provider_with_fake_completions(_FakeCompletions(chunks)), cancel)


def test_a_failure_nobody_cancelled_is_still_a_failure():
    chunks = [_Chunk([_Choice(content="x")]) for _ in range(5)]
    completions = _FakeCompletions(chunks, silent_after=1)
    provider = _provider_with_fake_completions(completions)
    # The connection drops on its own; Stop was never pressed.
    threading.Timer(0.3, lambda: completions.streams[0]._closed.set()).start()
    with pytest.raises(RuntimeError, match="closed stream"):
        _complete_with_cancel(provider, threading.Event())


def test_a_finished_call_leaves_no_watcher_thread_behind():
    provider = _provider_with_fake_completions(_FakeCompletions(_reply("done")))
    result = _complete_with_cancel(provider, threading.Event())
    assert result.text == "done"
    deadline = time.monotonic() + 2
    while any(t.name == "openai-cancel" for t in threading.enumerate()) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not any(t.name == "openai-cancel" for t in threading.enumerate())
