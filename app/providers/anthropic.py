"""The Anthropic Messages API directly (not the Claude Code CLI)."""

from __future__ import annotations

import logging
import os
import threading
from typing import cast

import anthropic
from anthropic.types import MessageParam

from .base import (
    Capability,
    ChatProvider,
    Completion,
    ConversationCancelled,
    ConversationError,
    close_when_cancelled,
    raise_if_token_cap_empty,
)

log = logging.getLogger(__name__)

_DEFAULT_API_KEY_ENV = "ANTHROPIC_API_KEY"


class AnthropicProvider(ChatProvider):
    name = "anthropic"
    # No JSON_SCHEMA: frontier models follow a plain "reply with this JSON
    # shape" instruction reliably enough that a real output_config.format
    # integration wasn't judged worth the added surface; extract_json_object()
    # is the fallback. CANCELLATION: the call is streamed so an abandoned one
    # can hang up (see complete). MODEL_LISTING: the Models API lists exactly
    # what this key may call, so the settings panel offers real ids instead of
    # a blank box. Typing an id from memory instead means a typo surfaces
    # only as a 404 on the first narration.
    capabilities = Capability.CANCELLATION | Capability.MODEL_LISTING

    def __init__(self, provider_config: dict, timeout: float) -> None:
        super().__init__(provider_config, timeout)
        self.model = provider_config.get("model", "claude-opus-5")
        api_key_env = provider_config.get("api_key_env", _DEFAULT_API_KEY_ENV)
        api_key = os.environ.get(api_key_env)
        if not api_key:
            raise ConversationError(
                f"{api_key_env} is not set — the conversation agent needs a "
                "real Anthropic API key (separate from the Claude Code CLI "
                "login the briefing agent uses), or switch conversation.provider "
                'to "ollama" in config.yaml to use local inference instead.'
            )
        self._client = anthropic.Anthropic(api_key=api_key)

    @classmethod
    def available(cls, provider_config: dict) -> bool:
        return bool(os.environ.get(provider_config.get("api_key_env", _DEFAULT_API_KEY_ENV)))

    @classmethod
    def list_models(cls, provider_config: dict) -> list[str]:
        """Models this key may call, newest first — the API's own order, which
        already puts the current generation at the top.

        Returns [] rather than raising when the key is missing: the settings
        panel must still open (and still show the ollama section) on a
        checkout that has no Anthropic key at all."""
        api_key = os.environ.get(provider_config.get("api_key_env", _DEFAULT_API_KEY_ENV))
        if not api_key:
            return []
        try:
            return [model.id for model in anthropic.Anthropic(api_key=api_key).models.list()]
        except anthropic.APIError as exc:
            raise ConversationError(f"Could not list Anthropic models: {exc}") from exc

    def prompt_tier(self) -> str:
        return "frontier"

    def complete(
        self,
        messages: list[dict],
        system: str,
        max_tokens: int,
        response_schema: dict | None,
        cancel: threading.Event | None,
    ) -> Completion:
        """Streamed, though the text is only returned once complete, so that an
        abandoned call can hang up. Stop cancels the awaiting asyncio task, but
        asyncio.to_thread cannot stop this thread: unstreamed, a stopped reply
        kept generating to the end — and was billed for it. Closing the stream
        closes the connection, which is what ends generation server-side.

        Closed by a watcher thread rather than only between events: current
        models reason before replying and send nothing while they do, so a
        between-events check could wait out the whole silence. Measured
        against the real SDK: close() from another thread returns within
        0.02 s whether events are flowing or the server is silent, and raises
        the HTTP library's own ReadError rather than an anthropic.APIError —
        hence the cancel check on any exception below."""
        done = threading.Event()
        try:
            # Passed as keywords rather than **a_dict: the SDK's signature is
            # overloaded, and a dict splat erases which argument is which -
            # every one of them then fails to type-check against every overload.
            with self._client.messages.stream(
                model=self.model,
                max_tokens=max_tokens,
                system=system,
                # cast, not a retype of the parameter: every provider takes the
                # same plain-dict message shape (see ChatProvider.complete), and
                # MessageParam is this SDK's name for it.
                messages=cast("list[MessageParam]", messages),
            ) as stream:
                if cancel is not None:
                    threading.Thread(
                        target=close_when_cancelled, args=(cancel, done, stream), daemon=True, name="anthropic-cancel"
                    ).start()
                try:
                    for _event in stream:
                        if cancel is not None and cancel.is_set():
                            raise ConversationCancelled("cancelled mid-stream")
                    response = stream.get_final_message()
                finally:
                    done.set()
        except ConversationCancelled:
            raise
        except anthropic.APIError as exc:
            if cancel is not None and cancel.is_set():
                raise ConversationCancelled("cancelled mid-stream") from exc
            raise ConversationError(f"Anthropic API call failed: {exc}") from exc
        except Exception as exc:
            # A stream closed by the watcher surfaces as the HTTP library's own
            # error; anything else unexpected is still a real failure.
            if cancel is not None and cancel.is_set():
                raise ConversationCancelled("cancelled mid-stream") from exc
            raise

        text = "".join(block.text for block in response.content if block.type == "text").strip()
        if response.stop_reason == "max_tokens":
            # Measured on claude-sonnet-5 at max_tokens=300: [thinking, text]
            # with the text cut mid-sentence, and on a longer prompt a thinking
            # block alone.
            raise_if_token_cap_empty(log, self.model, max_tokens, text, "conversation.anthropic.max_tokens")
        usage = response.usage
        return Completion(
            text=text,
            input_tokens=usage.input_tokens if usage else 0,
            output_tokens=usage.output_tokens if usage else 0,
        )
