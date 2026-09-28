"""The provider adapter seam — app/providers/ and ConversationClient's
delegation to it. Registry completeness, unknown-provider handling, and the
client-side policy that must hold for every provider (capability gating,
one capture per call, pre-dispatch cancellation), driven by a fake adapter
so nothing here touches a network or needs an API key."""

from __future__ import annotations

import threading

import pytest

from app.prompts import FRONTIER, SMALL, select_prompts
from app.providers import (
    DEFAULT_PROVIDER,
    REGISTRY,
    Capability,
    ChatProvider,
    Completion,
    ConversationCancelled,
    ConversationError,
    build_provider,
)
from app.services.conversation_service import ConversationClient, check_conversation_available


class _FakeProvider(ChatProvider):
    name = "fake"

    def __init__(self, capabilities: Capability = Capability.NONE, text: str = "reply") -> None:
        super().__init__({}, timeout=5)
        self.capabilities = capabilities
        self.model = "fake-model"
        self._text = text
        self.calls: list[dict] = []

    @classmethod
    def available(cls, provider_config: dict) -> bool:
        return True

    def prompt_tier(self) -> str:
        return "small"

    def complete(self, messages, system, max_tokens, response_schema, cancel) -> Completion:
        self.calls.append(
            {"messages": messages, "system": system, "max_tokens": max_tokens, "response_schema": response_schema}
        )
        return Completion(self._text, input_tokens=4, output_tokens=2)


def _client(provider: ChatProvider) -> ConversationClient:
    client = ConversationClient.__new__(ConversationClient)
    client._provider = provider
    client.max_tokens = 300
    client.total_input_tokens = 0
    client.total_output_tokens = 0
    client._inflight_cancel = None
    client._capture_dir = None
    client.prompts = type("Prompts", (), {"persona_system": "persona"})()
    return client


# --- registry ---


def test_default_provider_is_registered():
    assert DEFAULT_PROVIDER in REGISTRY


@pytest.mark.parametrize("name", sorted(REGISTRY))
def test_every_registered_provider_implements_the_interface(name):
    cls = REGISTRY[name]
    assert issubclass(cls, ChatProvider)
    assert cls.name == name
    assert not getattr(cls, "__abstractmethods__", None), f"{name} leaves abstract methods unimplemented"


@pytest.mark.parametrize("name", sorted(REGISTRY))
def test_providers_are_one_level_deep(name):
    """docs/STYLE.md: no concrete provider subclasses another."""
    assert REGISTRY[name].__bases__ == (ChatProvider,)


def test_unknown_provider_raises_naming_the_valid_options():
    with pytest.raises(ConversationError, match=r"unknown conversation\.provider 'nope'.*anthropic.*ollama"):
        build_provider({"provider": "nope"})


def test_unknown_provider_is_reported_unavailable_not_raised():
    assert check_conversation_available({"provider": "nope"}) is False


def test_build_provider_hands_each_adapter_only_its_own_block():
    provider = build_provider(
        {"provider": "ollama", "ollama": {"model": "m-local", "num_ctx": 4096}, "anthropic": {"model": "m-cloud"}}
    )
    assert provider.model == "m-local"
    assert provider.num_ctx == 4096


# --- ConversationClient policy, for any provider ---


def test_response_schema_is_dropped_for_a_provider_without_json_schema():
    provider = _FakeProvider(Capability.NONE)
    _client(provider).generate_once("p", system_prompt="s", response_schema={"type": "object"})
    assert provider.calls[0]["response_schema"] is None


def test_response_schema_is_passed_to_a_provider_with_json_schema():
    provider = _FakeProvider(Capability.JSON_SCHEMA)
    schema = {"type": "object"}
    _client(provider).generate_once("p", system_prompt="s", response_schema=schema)
    assert provider.calls[0]["response_schema"] == schema


def test_capture_runs_exactly_once_per_send(monkeypatch):
    client = _client(_FakeProvider())
    captured: list[tuple] = []
    monkeypatch.setattr(client, "_capture_call", lambda *args: captured.append(args))

    history, text = client._send([], "hello")

    assert len(captured) == 1
    assert text == "reply"
    assert history == [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "reply"}]


def test_send_does_not_mutate_the_callers_history():
    client = _client(_FakeProvider())
    original: list[dict] = [{"role": "user", "content": "earlier"}]
    client._send(original, "hello")
    assert original == [{"role": "user", "content": "earlier"}]


def test_a_call_cancelled_before_dispatch_never_reaches_the_provider():
    provider = _FakeProvider()
    client = _client(provider)
    cancel: threading.Event = client.arm_cancel()
    cancel.set()

    with pytest.raises(ConversationCancelled):
        client.generate_once("p", system_prompt="s")
    assert provider.calls == []


def test_empty_reply_raises_and_usage_is_not_counted():
    client = _client(_FakeProvider(text=""))
    with pytest.raises(ConversationError, match="fake returned no text content"):
        client.generate_once("p", system_prompt="s")
    assert (client.total_input_tokens, client.total_output_tokens) == (0, 0)


def test_client_exposes_the_adapters_identity():
    client = _client(_FakeProvider())
    assert (client.provider, client.model, client.num_ctx) == ("fake", "fake-model", None)


# --- prompt tier: config override vs the adapter's judgement ---


@pytest.mark.parametrize(
    ("configured", "provider_tier", "expected"),
    [
        ("auto", "small", SMALL),
        ("auto", "frontier", FRONTIER),
        (None, "frontier", FRONTIER),  # key absent means auto
        ("frontier", "small", FRONTIER),  # explicit config wins
        ("small", "frontier", SMALL),
        ("nonsense", "frontier", FRONTIER),  # unknown value falls back to auto
    ],
)
def test_select_prompts_prefers_explicit_config_then_the_provider(configured, provider_tier, expected):
    config = {} if configured is None else {"prompt_tier": configured}
    assert select_prompts(config, provider_tier) is expected
