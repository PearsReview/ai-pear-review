"""The adapter interface every model provider implements.

ConversationClient (app/services/conversation_service.py) owns conversation
policy — history, prompt assembly, usage counters, call capture. A provider
owns only transport: turning one vendor API into `complete(...)`. The client
holds a provider rather than subclassing per provider, so policy is written
once and a new provider is one adapter file plus one REGISTRY line.

One level deep: every concrete provider subclasses ChatProvider directly,
never another provider. An OpenAI-compatible server is one adapter
configured by base_url, not a subclass tree.
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Flag, auto

from ..services.errors import ServiceError

# The single home for conversation.provider's default. Every module that
# needs it imports this rather than repeating the literal.
DEFAULT_PROVIDER = "ollama"


class ConversationError(ServiceError):
    """Raised on any failure to get a response from the conversation provider."""


class ConversationCancelled(ConversationError):
    """Raised inside the worker thread when a call is abandoned mid-flight
    (see ConversationClient.arm_cancel). Almost never observed: by the time
    it's raised, the asyncio task that was awaiting this call has already
    been cancelled, so the future it would propagate through is gone and
    the exception is discarded. It exists so the abort path is explicit and
    distinguishable from a real provider failure in a traceback, not
    because callers are expected to catch it — a ConversationError subclass
    so any caller that *does* see one already handles it."""


class Capability(Flag):
    NONE = 0
    CANCELLATION = auto()  # can abandon a call mid-flight via the cancel token
    JSON_SCHEMA = auto()  # grammar-constrained decoding from a JSON Schema
    CONTEXT_WINDOW = auto()  # caller sets the context window size (num_ctx)
    MODEL_LISTING = auto()  # list_models() enumerates usable installed models


@dataclass(frozen=True)
class Completion:
    text: str
    input_tokens: int
    output_tokens: int


class ChatProvider(ABC):
    name: str
    capabilities: Capability = Capability.NONE

    def __init__(self, provider_config: dict, timeout: float) -> None:
        """provider_config is this provider's own block under
        `conversation:` (e.g. `conversation.ollama`)."""
        self.timeout = timeout
        self.model: str = ""
        self.num_ctx: int | None = None

    @classmethod
    @abstractmethod
    def available(cls, provider_config: dict) -> bool:
        """Cheap startup check — no client construction."""

    @classmethod
    def list_models(cls, provider_config: dict) -> list[str]:
        """Models the settings panel may offer. Only called when the provider
        declares Capability.MODEL_LISTING. Blocking — call off the event loop.

        Raises ConversationError if the provider could not be asked, the same
        as every other networked call here. The caller shows the panel without
        a model list rather than failing; that is its decision to make, and it
        can only make it if this reports the failure instead of swallowing it."""
        return []

    def prepare(self) -> None:  # noqa: B027 - an optional hook, not a missing implementation
        """One-time, possibly networked setup for the configured model (e.g.
        asking the server what the model can do). Called once at client
        construction, which runs off the event loop. Tolerates an unreachable
        server; raises ConversationError only for a definitive "this model
        can't be used"."""

    @abstractmethod
    def prompt_tier(self) -> str:
        """ "small" or "frontier" — which prompt set suits the configured model
        under `prompt_tier: auto`. Called after prepare()."""

    @abstractmethod
    def complete(
        self,
        messages: list[dict],
        system: str,
        max_tokens: int,
        response_schema: dict | None,
        cancel: threading.Event | None,
    ) -> Completion:
        """One call. response_schema is only ever passed when this provider
        declares Capability.JSON_SCHEMA; cancel may be ignored by a provider
        without Capability.CANCELLATION."""
