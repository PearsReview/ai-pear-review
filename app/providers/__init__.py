"""Model provider adapters — see base.py for the interface and why it's
composition rather than a per-provider ConversationClient subclass.

Adding a provider: one module implementing ChatProvider, one REGISTRY line.
"""

from __future__ import annotations

from .anthropic import AnthropicProvider
from .base import (
    DEFAULT_PROVIDER,
    Capability,
    ChatProvider,
    Completion,
    ConversationCancelled,
    ConversationError,
)
from .ollama import OllamaProvider
from .openai_compat import OpenAICompatProvider

REGISTRY: dict[str, type[ChatProvider]] = {
    OllamaProvider.name: OllamaProvider,
    AnthropicProvider.name: AnthropicProvider,
    OpenAICompatProvider.name: OpenAICompatProvider,
}


def provider_class(conversation_config: dict) -> type[ChatProvider]:
    """conversation_config is config.yaml's whole `conversation:` block."""
    name = conversation_config.get("provider", DEFAULT_PROVIDER)
    try:
        return REGISTRY[name]
    except KeyError:
        known = ", ".join(sorted(REGISTRY))
        raise ConversationError(f"unknown conversation.provider {name!r} — expected one of: {known}") from None


def build_provider(conversation_config: dict) -> ChatProvider:
    cls = provider_class(conversation_config)
    return cls(conversation_config.get(cls.name, {}), conversation_config.get("timeout_seconds", 60))


__all__ = [
    "DEFAULT_PROVIDER",
    "REGISTRY",
    "AnthropicProvider",
    "Capability",
    "ChatProvider",
    "Completion",
    "ConversationCancelled",
    "ConversationError",
    "OllamaProvider",
    "OpenAICompatProvider",
    "build_provider",
    "provider_class",
]
