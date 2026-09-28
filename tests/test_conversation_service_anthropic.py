"""The Anthropic path — ConversationClient construction, the adapter's
single-call plumbing (AnthropicProvider.complete), and generate_once's
documented "no schema constraint on this provider" behavior.

This had ZERO test coverage before: every existing test either bypasses
__init__ entirely via ConversationClient.__new__ to test pure prompt
assembly (see test_hunk_prompt_briefing.py), or drives the ollama path.
qa_agent/llm_client.py's own docstring already flags that nothing in this
repo's automated suite has ever run the app itself configured with
provider: anthropic — these are mocked unit tests (no ANTHROPIC_API_KEY
available in this environment to go further), but they're real coverage
of code that had none: the missing-API-key error message a user
misconfiguring Anthropic will actually see, and the request/response
shape AnthropicProvider.complete builds and parses.
"""

from __future__ import annotations

import threading
import time

import pytest
from anthropic import APIError

from app.providers import AnthropicProvider
from app.providers.base import Capability, ConversationCancelled
from app.services.conversation_service import (
    ConversationClient,
    ConversationError,
    check_conversation_available,
)


def _anthropic_config(**overrides) -> dict:
    config = {"provider": "anthropic", "max_tokens": 300, "anthropic": {"model": "claude-opus-5"}}
    config.update(overrides)
    return config


class _TextBlock:
    def __init__(self, text: str, type: str = "text") -> None:
        self.text = text
        self.type = type


class _Usage:
    def __init__(self, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _Response:
    def __init__(self, content: list, usage: _Usage | None = None, stop_reason: str = "end_turn") -> None:
        self.content = content
        self.usage = usage
        # Always present on a real Message, so the fake carries it too —
        # complete() reads it to catch a reply cut off at the token cap.
        self.stop_reason = stop_reason


class _FakeStream:
    """Stands in for the SDK's MessageStream, matching what was measured on the
    real one: an API error is raised on entering, events arrive while iterating,
    and close() from another thread makes a blocked read raise."""

    def __init__(self, response, error: Exception | None, events: int, silent_after: int | None) -> None:
        self._response = response
        self._error = error
        self._events = events
        self._silent_after = silent_after
        self._closed = threading.Event()
        self.closed_by_watcher = False

    def __enter__(self):
        if self._error is not None:
            raise self._error
        return self

    def __exit__(self, *exc_info) -> None:
        self._closed.set()

    def __iter__(self):
        for i in range(self._events):
            if self._silent_after is not None and i == self._silent_after:
                # The model reasoning: nothing arrives until the connection is closed.
                if not self._closed.wait(10):
                    raise AssertionError("the silent stream was never closed")
                raise RuntimeError("read on a closed stream")  # the HTTP library's ReadError, in the real SDK
            yield {"type": "content_block_delta"}

    def close(self) -> None:
        self.closed_by_watcher = True
        self._closed.set()

    def get_final_message(self):
        return self._response


class _FakeMessages:
    """Records the call it received and hands back whatever was queued —
    the same "spy plus canned response" shape as the STT/TTS fakes in
    tests/test_voice_service.py."""

    def __init__(
        self, response=None, error: Exception | None = None, events: int = 2, silent_after: int | None = None
    ) -> None:
        self._response = response
        self._error = error
        self._events = events
        self._silent_after = silent_after
        self.calls: list[dict] = []
        self.streams: list[_FakeStream] = []

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        stream = _FakeStream(self._response, self._error, self._events, self._silent_after)
        self.streams.append(stream)
        return stream


def _provider_with_fake_messages(messages: _FakeMessages) -> AnthropicProvider:
    """__new__-bypass so no API key or real SDK client is needed — sets
    exactly what complete() touches."""
    provider = AnthropicProvider.__new__(AnthropicProvider)
    provider.model = "claude-opus-5"
    provider.num_ctx = None
    provider._client = type("FakeAnthropicClient", (), {"messages": messages})()
    return provider


def _client_with_fake_messages(messages: _FakeMessages, **config_overrides) -> ConversationClient:
    """Same __new__-bypass idiom test_hunk_prompt_briefing.py already
    uses, extended to set exactly what generate_once actually touches."""
    client = ConversationClient.__new__(ConversationClient)
    client._provider = _provider_with_fake_messages(messages)
    client.max_tokens = config_overrides.get("max_tokens", 300)
    client.total_input_tokens = 0
    client.total_output_tokens = 0
    client._inflight_cancel = None
    client._capture_dir = None
    return client


def _complete(provider: AnthropicProvider, messages: list[dict], system: str, max_tokens: int = 300):
    result = provider.complete(messages, system, max_tokens, None, None)
    return result.text, result.input_tokens, result.output_tokens


# --- ConversationClient.__init__ (provider="anthropic") ---


def test_missing_api_key_raises_a_clear_conversation_error(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ConversationError, match="ANTHROPIC_API_KEY is not set"):
        ConversationClient(_anthropic_config())


# --- the reply-token cap (shared with the model's own reasoning) ---


def test_a_reply_cut_off_at_the_cap_is_still_returned():
    """Truncated prose is worth more to the reviewer than an error — the
    warning goes to the log, not in their way."""
    messages = _FakeMessages(_Response([_TextBlock("It works because the sandbox")], _Usage(10, 300), "max_tokens"))
    text, _, _ = _complete(_provider_with_fake_messages(messages), [{"role": "user", "content": "why?"}], "sys")
    assert text == "It works because the sandbox"


def test_reasoning_eating_the_whole_cap_names_the_setting_to_raise():
    """Measured on claude-sonnet-5 at max_tokens=300: the response came back
    as a thinking block with no text at all. The old message for this was
    "anthropic returned no text content", which sent you looking at the
    prompt rather than at the budget."""
    messages = _FakeMessages(_Response([], _Usage(10, 300), "max_tokens"))
    provider = _provider_with_fake_messages(messages)
    with pytest.raises(ConversationError, match=r"conversation\.anthropic\.max_tokens"):
        _complete(provider, [{"role": "user", "content": "why?"}], "sys")


def test_the_provider_block_max_tokens_wins_over_the_shared_one(monkeypatch):
    """300 suits the local model; Anthropic shares that budget with its own
    reasoning and needs more, so its block overrides rather than the two
    providers fighting over one number."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setattr("app.providers.anthropic.anthropic.Anthropic", lambda api_key: object())
    client = ConversationClient(
        _anthropic_config(max_tokens=300, anthropic={"model": "claude-sonnet-5", "max_tokens": 1024})
    )
    assert client.max_tokens == 1024


def test_the_shared_max_tokens_still_applies_when_the_block_has_none(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setattr("app.providers.anthropic.anthropic.Anthropic", lambda api_key: object())
    client = ConversationClient(_anthropic_config(max_tokens=300))
    assert client.max_tokens == 300


# --- list_models (settings panel's model choices) ---


def test_list_models_returns_the_ids_the_key_can_call(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    listed = [type("M", (), {"id": "claude-opus-5"})(), type("M", (), {"id": "claude-sonnet-5"})()]
    monkeypatch.setattr(
        "app.providers.anthropic.anthropic.Anthropic",
        lambda api_key: type("C", (), {"models": type("Models", (), {"list": lambda self: listed})()})(),
    )
    assert AnthropicProvider.list_models({}) == ["claude-opus-5", "claude-sonnet-5"]


def test_list_models_is_empty_without_a_key_rather_than_raising(monkeypatch):
    """The settings panel must still open on a checkout with no Anthropic
    key — handle_get_settings logs and moves on, but only if nothing here
    raises before it can."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert AnthropicProvider.list_models({}) == []


def test_missing_api_key_error_names_a_custom_env_var():
    """Mirrors tests/test_preflight.py's own "reports the key it actually
    looks for" pin — api_key_env is configurable, and the error the user
    sees for a misconfigured Anthropic setup must name the variable this
    config actually reads, not always the literal default."""
    config = _anthropic_config(anthropic={"model": "claude-opus-5", "api_key_env": "MY_KEY"})
    with pytest.raises(ConversationError, match="MY_KEY is not set"):
        ConversationClient(config)


def test_constructs_successfully_with_a_key_present(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake-test-key")
    client = ConversationClient(_anthropic_config())
    assert client.provider == "anthropic"
    assert client.model == "claude-opus-5"
    # Anthropic manages its own context window — nothing here should ever
    # be sent as a num_ctx-equivalent (see conversation_service.py's own
    # comment on this and this session's settings-panel gating for it).
    assert client.num_ctx is None
    assert client._provider._client is not None


def test_defaults_the_model_when_anthropic_block_is_missing_the_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake-test-key")
    client = ConversationClient({"provider": "anthropic"})
    assert client.model  # some sane default, not a crash or an empty string


# --- check_conversation_available (anthropic branch — no network) ---


def test_check_conversation_available_true_when_key_is_set(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake-test-key")
    assert check_conversation_available({"provider": "anthropic"}) is True


def test_check_conversation_available_false_when_key_is_absent(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert check_conversation_available({"provider": "anthropic"}) is False


def test_check_conversation_available_honors_a_custom_env_var_name(monkeypatch):
    monkeypatch.delenv("SOME_OTHER_KEY", raising=False)
    config = {"provider": "anthropic", "anthropic": {"api_key_env": "SOME_OTHER_KEY"}}
    assert check_conversation_available(config) is False
    monkeypatch.setenv("SOME_OTHER_KEY", "sk-ant-fake-test-key")
    assert check_conversation_available(config) is True


# --- AnthropicProvider.complete ---


def test_anthropic_complete_builds_the_right_request_and_parses_the_reply():
    messages = _FakeMessages(_Response([_TextBlock("Looks like a simple guard clause.")], _Usage(120, 18)))
    provider = _provider_with_fake_messages(messages)

    text, in_tokens, out_tokens = _complete(
        provider, [{"role": "user", "content": "What changed here?"}], "You are a reviewer."
    )

    assert text == "Looks like a simple guard clause."
    assert (in_tokens, out_tokens) == (120, 18)
    assert len(messages.calls) == 1
    call = messages.calls[0]
    assert call["model"] == "claude-opus-5"
    assert call["system"] == "You are a reviewer."
    assert call["messages"] == [{"role": "user", "content": "What changed here?"}]
    assert call["max_tokens"] == 300


def test_generate_once_falls_back_to_the_client_max_tokens():
    messages = _FakeMessages(_Response([_TextBlock("ok")], _Usage(1, 1)))
    client = _client_with_fake_messages(messages)

    client.generate_once("hi", system_prompt="sys")

    assert messages.calls[0]["max_tokens"] == 300


def test_generate_once_prefers_an_explicit_max_tokens_over_the_client_default():
    messages = _FakeMessages(_Response([_TextBlock("ok")], _Usage(1, 1)))
    client = _client_with_fake_messages(messages)

    client.generate_once("hi", system_prompt="sys", max_tokens=64)

    assert messages.calls[0]["max_tokens"] == 64


def test_anthropic_complete_joins_only_text_blocks_in_order():
    """A real Messages API response can carry non-text content blocks
    (tool_use, thinking, etc.) even though this app never requests tools
    on this path — the filter in AnthropicProvider.complete must skip anything that
    isn't type == "text" rather than crashing on a block with no .text."""
    blocks = [_TextBlock("Part one. "), _TextBlock("", type="tool_use"), _TextBlock("Part two.")]
    messages = _FakeMessages(_Response(blocks, _Usage(5, 5)))
    provider = _provider_with_fake_messages(messages)

    text, _, _ = _complete(provider, [{"role": "user", "content": "hi"}], "sys")

    assert text == "Part one. Part two."


def test_anthropic_complete_defaults_tokens_to_zero_when_usage_is_missing():
    messages = _FakeMessages(_Response([_TextBlock("ok")], usage=None))
    provider = _provider_with_fake_messages(messages)

    _, in_tokens, out_tokens = _complete(provider, [{"role": "user", "content": "hi"}], "sys")

    assert (in_tokens, out_tokens) == (0, 0)


# --- Stop: an abandoned call hangs up instead of generating to the end ---


def _complete_with_cancel(provider: AnthropicProvider, cancel: threading.Event):
    return provider.complete([{"role": "user", "content": "hi"}], "sys", 300, None, cancel)


def test_the_provider_declares_cancellation():
    assert AnthropicProvider.capabilities & Capability.CANCELLATION


def test_stop_hangs_up_a_silent_stream_promptly():
    """The case a between-events check can't reach: the model reasoning, with
    nothing on the wire. Before this, a stopped reply ran to the end and was
    billed; now the watcher closes the connection within a poll interval."""
    messages = _FakeMessages(_Response([_TextBlock("never")], _Usage(1, 1)), events=5, silent_after=1)
    provider = _provider_with_fake_messages(messages)
    cancel = threading.Event()
    threading.Timer(0.3, cancel.set).start()
    started = time.monotonic()

    with pytest.raises(ConversationCancelled):
        _complete_with_cancel(provider, cancel)
    assert time.monotonic() - started < 2
    assert messages.streams[0].closed_by_watcher


def test_stop_while_events_are_flowing_is_noticed_between_them():
    messages = _FakeMessages(_Response([_TextBlock("never")], _Usage(1, 1)), events=50)
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(ConversationCancelled):
        _complete_with_cancel(_provider_with_fake_messages(messages), cancel)


def test_a_failure_nobody_cancelled_is_still_a_failure():
    """The broad except only turns errors into a cancel when Stop was pressed."""
    messages = _FakeMessages(_Response([_TextBlock("x")], _Usage(1, 1)), events=5, silent_after=1)
    provider = _provider_with_fake_messages(messages)
    # The connection drops on its own; Stop was never pressed.
    threading.Timer(0.3, lambda: messages.streams[0]._closed.set()).start()
    with pytest.raises(RuntimeError, match="closed stream"):
        _complete_with_cancel(provider, threading.Event())


def test_a_finished_call_leaves_no_watcher_thread_behind():
    messages = _FakeMessages(_Response([_TextBlock("done")], _Usage(1, 1)))
    result = _complete_with_cancel(_provider_with_fake_messages(messages), threading.Event())
    assert result.text == "done"
    deadline = time.monotonic() + 2
    while any(t.name == "anthropic-cancel" for t in threading.enumerate()) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not any(t.name == "anthropic-cancel" for t in threading.enumerate())


def test_anthropic_complete_wraps_api_error_as_conversation_error():
    # APIError.__init__ only ever stores `request` (see its source — no
    # validation, no attribute access on it), so a plain sentinel avoids
    # coupling this test to anthropic's own vendored HTTP client package
    # name/version (currently "httpx2", not the real httpx — an internal
    # detail of anthropic==1.0.0 that could change independently of
    # anything this app does).
    api_error = APIError("rate limited", request=object(), body=None)
    messages = _FakeMessages(error=api_error)
    provider = _provider_with_fake_messages(messages)

    with pytest.raises(ConversationError, match="Anthropic API call failed"):
        _complete(provider, [{"role": "user", "content": "hi"}], "sys")


# --- generate_once (anthropic path) — the JSON-constraint gap this test file exists for ---


def test_generate_once_does_not_forward_response_schema_to_anthropic():
    """Pins the behavior README.md now documents explicitly: unlike the
    Ollama path (which passes response_schema through as `format` —
    grammar-constrained decoding), Anthropic gets no schema-related
    request field at all. Briefing/Act Now rely on prompting plus
    extract_json_object (tests/test_json_utils.py) as the fallback on
    this provider — this test is what would catch it silently drifting
    either direction (schema silently starting to be sent with no
    Anthropic-side support for it, or the fallback assumption becoming
    untrue some other way)."""
    messages = _FakeMessages(_Response([_TextBlock('{"intent": "adds a guard clause"}')], _Usage(10, 10)))
    client = _client_with_fake_messages(messages)

    text = client.generate_once(
        "Explain this hunk.",
        system_prompt="You write JSON briefings.",
        max_tokens=200,
        response_schema={"type": "object", "properties": {"intent": {"type": "string"}}},
    )

    assert text == '{"intent": "adds a guard clause"}'
    call = messages.calls[0]
    assert "format" not in call
    assert "response_schema" not in call
    assert "tools" not in call


def test_generate_once_raises_on_empty_reply():
    messages = _FakeMessages(_Response([_TextBlock("")], _Usage(5, 0)))
    client = _client_with_fake_messages(messages)

    with pytest.raises(ConversationError, match="returned no text content"):
        client.generate_once("Explain this hunk.", system_prompt="sys")


def test_generate_once_accumulates_usage_counters():
    messages = _FakeMessages(_Response([_TextBlock("ok")], _Usage(7, 3)))
    client = _client_with_fake_messages(messages)

    client.generate_once("first", system_prompt="sys")
    client.generate_once("second", system_prompt="sys")

    assert client.total_input_tokens == 14
    assert client.total_output_tokens == 6
