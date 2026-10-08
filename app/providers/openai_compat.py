"""Any OpenAI-compatible chat endpoint, picked by base_url — a self-hosted
server, or a proxy/gateway (LiteLLM, vLLM, an enterprise LLM gateway) that
fronts several model families behind one OpenAI-shaped API. One adapter, not a
subclass per vendor: the wire format is OpenAI's `/chat/completions`, and the
model id (e.g. "anthropic--claude-4.6-sonnet", "gpt-5", "gemini-2.5-pro")
selects what the proxy routes to.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import TYPE_CHECKING, cast

# Optional until someone picks this provider: providers/__init__ imports this
# module unconditionally, and a venv made before openai joined
# requirements.txt (the VS Code extension's managed one) must still start the
# backend for an Ollama or Anthropic user. _require_openai() raises the
# actionable error instead.
try:
    import openai
except ImportError:
    openai = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from openai.types.chat import ChatCompletionMessageParam

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

_DEFAULT_API_KEY_ENV = "OPENAI_API_KEY"


def _require_openai() -> None:
    if openai is None:
        raise ConversationError(
            "The openai package is not installed — run `pip install -r requirements.txt` "
            "(in VS Code: Pear Review: Set Up Python Environment) to use the OpenAI-compatible provider."
        )


class OpenAICompatProvider(ChatProvider):
    name = "openai"
    # No JSON_SCHEMA: the proxy fronts many models with uneven schema support,
    # so narration relies on the plain "reply with this JSON shape" instruction
    # (extract_json_object() is the fallback), as the Anthropic adapter does.
    # CANCELLATION: the call is streamed so an abandoned one can hang up (see
    # complete). MODEL_LISTING: /v1/models reports exactly what this endpoint
    # routes, so the settings panel offers real ids instead of a blank box.
    capabilities = Capability.CANCELLATION | Capability.MODEL_LISTING

    def __init__(self, provider_config: dict, timeout: float) -> None:
        super().__init__(provider_config, timeout)
        _require_openai()
        self.model = provider_config.get("model") or ""
        if not self.model:
            raise ConversationError(
                "conversation.openai.model is not set — pick a model in the settings "
                "panel or app/config.yaml (the panel lists what the endpoint reports)."
            )
        self.base_url = (provider_config.get("base_url") or "").strip() or None
        api_key_env = provider_config.get("api_key_env", _DEFAULT_API_KEY_ENV)
        api_key = os.environ.get(api_key_env)
        if not api_key:
            raise ConversationError(
                f"{api_key_env} is not set — the OpenAI-compatible endpoint needs an "
                "API key. Set it in the settings panel (the key for your proxy/gateway), "
                'or switch conversation.provider to "ollama" in config.yaml for local inference.'
            )
        self._client = openai.OpenAI(api_key=api_key, base_url=self.base_url, timeout=timeout)

    @classmethod
    def available(cls, provider_config: dict) -> bool:
        return openai is not None and bool(os.environ.get(provider_config.get("api_key_env", _DEFAULT_API_KEY_ENV)))

    @classmethod
    def list_models(cls, provider_config: dict) -> list[str]:
        """Model ids the endpoint reports, sorted. Returns [] rather than
        raising when the key is missing, so the panel still opens on a checkout
        that has no key set yet (mirrors the Anthropic adapter)."""
        api_key = os.environ.get(provider_config.get("api_key_env", _DEFAULT_API_KEY_ENV))
        if not api_key:
            return []
        _require_openai()
        base_url = (provider_config.get("base_url") or "").strip() or None
        try:
            client = openai.OpenAI(api_key=api_key, base_url=base_url, timeout=15)
            return sorted(model.id for model in client.models.list())
        except openai.APIError as exc:
            raise ConversationError(f"Could not list models from the OpenAI-compatible endpoint: {exc}") from exc

    def prompt_tier(self) -> str:
        # A proxy/gateway reviewer model is a frontier model (Claude, GPT, Gemini);
        # the frontier prompts suit it, the same choice the Anthropic adapter makes.
        return "frontier"

    def complete(
        self,
        messages: list[dict],
        system: str,
        max_tokens: int,
        response_schema: dict | None,
        cancel: threading.Event | None,
    ) -> Completion:
        """Streamed so an abandoned call can hang up: the caller runs inside
        asyncio.to_thread, whose cancellation does not stop this thread, and an
        unstreamed call would keep generating (and billing) to the end. Closing
        the stream closes the connection, which is what ends generation. A
        watcher thread closes it the moment cancel is set rather than only
        between chunks, since a reasoning model can be silent for a while before
        the first token (mirrors the Anthropic adapter)."""
        done = threading.Event()
        # OpenAI's API has no top-level system field; it is the first message.
        # cast, not a retype: every provider takes the same plain role/content
        # dict shape (see ChatProvider.complete); this is the SDK's name for it.
        full_messages = cast("list[ChatCompletionMessageParam]", [{"role": "system", "content": system}, *messages])
        parts: list[str] = []
        in_tokens = 0
        out_tokens = 0
        finish_reason: str | None = None
        try:
            stream = self._client.chat.completions.create(
                model=self.model,
                messages=full_messages,
                max_tokens=max_tokens,
                stream=True,
                stream_options={"include_usage": True},
            )
            if cancel is not None:
                threading.Thread(
                    target=close_when_cancelled, args=(cancel, done, stream), daemon=True, name="openai-cancel"
                ).start()
            try:
                for chunk in stream:
                    if cancel is not None and cancel.is_set():
                        raise ConversationCancelled("cancelled mid-stream")
                    for choice in chunk.choices:
                        if choice.delta and choice.delta.content:
                            parts.append(choice.delta.content)
                        if choice.finish_reason:
                            finish_reason = choice.finish_reason
                    # Usage arrives on a final chunk (include_usage) whose choices are empty.
                    if chunk.usage:
                        in_tokens = chunk.usage.prompt_tokens
                        out_tokens = chunk.usage.completion_tokens
            finally:
                done.set()
                stream.close()
        except ConversationCancelled:
            raise
        except openai.APIError as exc:
            if cancel is not None and cancel.is_set():
                raise ConversationCancelled("cancelled mid-stream") from exc
            raise ConversationError(f"OpenAI-compatible API call failed: {exc}") from exc
        except Exception as exc:
            # A stream closed by the watcher surfaces as the HTTP library's own
            # error; anything else unexpected is still a real failure.
            if cancel is not None and cancel.is_set():
                raise ConversationCancelled("cancelled mid-stream") from exc
            raise

        text = "".join(parts).strip()
        if finish_reason == "length":
            raise_if_token_cap_empty(log, self.model, max_tokens, text, "conversation.openai.max_tokens")
        return Completion(text=text, input_tokens=in_tokens, output_tokens=out_tokens)
