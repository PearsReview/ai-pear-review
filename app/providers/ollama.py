"""A local Ollama server — no API key, no cloud call, $0 marginal cost.

Any model Ollama serves can be configured: at client construction the
adapter asks the server what the model is (`/api/show` — capabilities,
native context length, parameter count) and adapts to it, rather than
assuming the model this app was tuned on. See _ModelInfo's uses below.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Any

import requests

from .base import Capability, ChatProvider, Completion, ConversationCancelled, ConversationError

_DEFAULT_BASE_URL = "http://127.0.0.1:11434"

# The "connect" half of requests' (connect, read) timeout tuple — how long to
# wait for the TCP handshake, not for a response. Kept short and fixed
# regardless of timeout_seconds (that governs the read half and the overall
# deadline): a local Ollama server that isn't even accepting connections yet
# should fail fast, not wait out the budget given to an in-progress generation.
_CONNECT_TIMEOUT_SECONDS = 5

# /api/show is a metadata read, not a generation — it answers in well under a
# second locally. Short so an unresponsive server can't stall construction.
_SHOW_TIMEOUT_SECONDS = 5

# Under `prompt_tier: auto`, models at or above this many parameters get the
# frontier prompts. The old name-based guess only recognised 70b/72b/90b/405b;
# 60 keeps that boundary with margin for how Ollama reports sizes ("70.6B"),
# and now also catches models whose names carry no size (gpt-oss:120b,
# deepseek-r1:671b, mixtral:8x22b). Total, not active, parameters for MoE.
_FRONTIER_MIN_PARAMS_B = 60.0

# Fallback when the server can't be asked: the size markers the name-based
# guess always used.
_FRONTIER_NAME_HINTS = ("70b", "72b", "90b", "405b")

# gpt-oss can't turn reasoning off — Ollama ignores think=false for it and
# accepts only "low"/"medium"/"high" — so its trace always spends tokens from
# the same num_predict budget as the answer. This is added on top so a
# 300-token narration still has room to be written. A guess, not measured on
# this app: if gpt-oss replies come back "spent its whole budget reasoning",
# raise it.
_ALWAYS_ON_REASONING_ALLOWANCE = 1024

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class _ModelInfo:
    capabilities: frozenset[str]  # e.g. {"completion", "tools", "thinking"}
    context_length: int | None  # the model's native window
    parameter_billions: float | None
    family: str


def _parse_parameter_billions(size: str | None) -> float | None:
    """Ollama reports sizes as "7.6B", "116.8B", "567M"."""
    match = re.fullmatch(r"\s*([\d.]+)\s*([BMK])\s*", size or "", re.IGNORECASE)
    if not match:
        return None
    scale = {"B": 1.0, "M": 1e-3, "K": 1e-6}[match.group(2).upper()]
    return float(match.group(1)) * scale


def _fetch_model_info(base_url: str, model: str) -> _ModelInfo | None:
    """Asks the server what it knows about a model.

    Returns None when the server can't be reached or doesn't know the
    model, which callers treat as "no information" rather than an error. A
    missing model is preflight's job to report, and the first real call
    surfaces Ollama's own error."""
    try:
        resp = requests.post(f"{base_url}/api/show", json={"model": model}, timeout=_SHOW_TIMEOUT_SECONDS)
        if not resp.ok:
            return None
        data = resp.json()
    except (requests.RequestException, ValueError):
        return None
    model_info = data.get("model_info") or {}
    context_length = next(
        (v for k, v in model_info.items() if k.endswith(".context_length") and isinstance(v, int)), None
    )
    details = data.get("details") or {}
    return _ModelInfo(
        capabilities=frozenset(data.get("capabilities") or ()),
        context_length=context_length,
        parameter_billions=_parse_parameter_billions(details.get("parameter_size")),
        family=str(details.get("family") or ""),
    )


def _can_chat(info: _ModelInfo | None) -> bool:
    # An empty capability list means a server that doesn't report them — not
    # evidence the model can't chat.
    return info is None or not info.capabilities or "completion" in info.capabilities


class OllamaProvider(ChatProvider):
    name = "ollama"
    capabilities = (
        Capability.CANCELLATION | Capability.JSON_SCHEMA | Capability.CONTEXT_WINDOW | Capability.MODEL_LISTING
    )

    def __init__(self, provider_config: dict, timeout: float) -> None:
        super().__init__(provider_config, timeout)
        # No code-side default model: config.yaml is the one home for it, and
        # preflight already treats an unset model as a failure.
        self.model = provider_config.get("model") or ""
        if not self.model:
            raise ConversationError(
                "conversation.ollama.model is not set — pick an installed model in the "
                "settings panel or app/config.yaml (`ollama list` shows what's installed)."
            )
        self.base_url = provider_config.get("base_url") or _DEFAULT_BASE_URL
        # None means "don't send num_ctx", leaving Ollama's own default in
        # force — kept possible on purpose, but config.yaml sets it explicitly
        # because that default is a bad thing to inherit (see its num_ctx note).
        self.num_ctx = provider_config.get("num_ctx")
        self._info: _ModelInfo | None = None
        self._prepared = False

    @classmethod
    def available(cls, provider_config: dict) -> bool:
        """An actual (short-timeout) reachability check, since "is it
        running" isn't knowable from config alone."""
        base_url = provider_config.get("base_url") or _DEFAULT_BASE_URL
        try:
            return requests.get(f"{base_url}/api/tags", timeout=2).ok
        except requests.RequestException:
            return False

    @classmethod
    def list_models(cls, provider_config: dict) -> list[str]:
        """Installed models that can hold a conversation — embedding-only
        models install alongside chat models but would fail on first use,
        so they're not offered."""
        base_url = provider_config.get("base_url") or _DEFAULT_BASE_URL
        try:
            resp = requests.get(f"{base_url}/api/tags", timeout=_SHOW_TIMEOUT_SECONDS)
            resp.raise_for_status()
            names = sorted(m.get("name", "") for m in resp.json().get("models", []) if m.get("name"))
        except (requests.RequestException, ValueError) as exc:
            raise ConversationError(f"Could not list Ollama models: {exc}") from exc
        return [name for name in names if _can_chat(_fetch_model_info(base_url, name))]

    def prepare(self) -> None:
        if self._prepared:
            return
        self._prepared = True
        self._info = _fetch_model_info(self.base_url, self.model)
        if self._info is None:
            return
        if not _can_chat(self._info):
            raise ConversationError(
                f"{self.model} can't hold a conversation — Ollama reports its capabilities as "
                f'{sorted(self._info.capabilities)}, with no "completion". Pick a chat model.'
            )
        native = self._info.context_length
        if self.num_ctx and native and self.num_ctx > native:
            # More than the model was trained on buys no usable context, and
            # app/web/context.py budgets prompts against num_ctx — so the budget must
            # reflect what the model can actually attend to.
            log.info(
                "num_ctx %d exceeds %s's native %d-token context; using %d",
                self.num_ctx,
                self.model,
                native,
                native,
            )
            self.num_ctx = native

    def prompt_tier(self) -> str:
        if self._info is not None and self._info.parameter_billions is not None:
            return "frontier" if self._info.parameter_billions >= _FRONTIER_MIN_PARAMS_B else "small"
        lowered = self.model.lower()
        return "frontier" if any(hint in lowered for hint in _FRONTIER_NAME_HINTS) else "small"

    def _reasoning_always_on(self) -> bool:
        family = self._info.family.replace("-", "").lower() if self._info else ""
        return self.model.lower().startswith("gpt-oss") or family == "gptoss"

    def complete(
        self,
        messages: list[dict],
        system: str,
        max_tokens: int,
        response_schema: dict | None,
        cancel: threading.Event | None,
    ) -> Completion:
        """max_tokens maps to Ollama's `options.num_predict` — a real hard cap
        on generation length. Don't drop it: caught live, on this app's own
        default model, schema-constrained generation against a hunk the
        instruction didn't match ran far past any reasonable length, and
        `requests`' timeout never caught it because Ollama kept trickling bytes
        (that timeout is measured between reads, not over the whole request).
        Each hung call permanently occupied an asyncio.to_thread worker; enough
        of them starved the pool for every other connection. num_predict makes
        the failure "output is unusable" instead of "request never returns".

        Streamed even though the text is only returned once complete, because
        chunk boundaries are the only place this code can notice it has been
        abandoned. The caller runs inside asyncio.to_thread, whose cancellation
        does not stop this thread. Unstreamed, a cancelled call holds its
        slot in Ollama's serialized per-model queue for its full budget, and
        the next real request queues behind it invisibly — the intermittent
        Act Now hang. Leaving the `with` block closes the connection, which
        tells Ollama to stop generating.

        Two bounds, because they catch different failures: `deadline` is
        wall-clock time across the whole call, while the read timeout catches
        a connection that goes fully silent mid-stream.

        Known residual: a call cancelled while still *queued* at Ollama has
        produced no bytes, so there's no chunk boundary to notice at, and it
        holds its slot until the first token or the read timeout. Bounded, and
        not self-sustaining — it takes an existing backlog to happen at all.

        Reasoning models: Ollama enables thinking by default and streams the
        trace in `message.thinking`, separate from `content` — so it is never
        returned (or spoken), but it spends num_predict and wall-clock time
        before any answer. Turned off where the model allows it; see
        _ALWAYS_ON_REASONING_ALLOWANCE for the model that doesn't. `think` is
        only sent to models reporting "thinking": Ollama rejects think=true
        with an error for any other model."""
        self.prepare()
        payload: dict[str, Any] = {
            "model": self.model,
            # /api/chat has no top-level `system` field, so the system prompt
            # is prepended fresh each call rather than stored in history.
            "messages": [{"role": "system", "content": system}] + messages,
            "stream": True,  # for cancellation, not latency — see docstring
        }
        if response_schema is not None:
            payload["format"] = response_schema  # grammar-constrained decoding
        if self._info is not None and "thinking" in self._info.capabilities:
            if self._reasoning_always_on():
                payload["think"] = "low"
                max_tokens += _ALWAYS_ON_REASONING_ALLOWANCE
            else:
                payload["think"] = False
        options = {"num_predict": max_tokens}
        if self.num_ctx:
            # Sent on every call: Ollama's default is 2048 whatever the model
            # supports, and it silently drops the FRONT of an over-long prompt
            # (see config.yaml's num_ctx note for the measurement).
            options["num_ctx"] = self.num_ctx
        payload["options"] = options

        deadline = time.monotonic() + self.timeout
        parts: list[str] = []
        reasoned = False
        in_tokens = 0
        out_tokens = 0
        try:
            with requests.post(
                f"{self.base_url}/api/chat",
                json=payload,
                timeout=(_CONNECT_TIMEOUT_SECONDS, self.timeout),
                stream=True,
            ) as resp:
                resp.raise_for_status()
                for line in resp.iter_lines(decode_unicode=True):
                    if cancel is not None and cancel.is_set():
                        # Leaving this `with` closes the connection, which is
                        # what actually stops generation server-side.
                        raise ConversationCancelled("cancelled mid-stream")
                    if time.monotonic() > deadline:
                        raise ConversationError(f"Ollama call exceeded its {self.timeout}s budget before finishing")
                    if not line:
                        continue  # Ollama separates JSON objects with blank lines
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise ConversationError(f"Ollama sent a malformed stream chunk: {exc}") from exc
                    if chunk.get("error"):
                        # Reported in-band with HTTP 200 already sent —
                        # raise_for_status above cannot catch these.
                        raise ConversationError(f"Ollama call failed: {chunk['error']}")
                    message = chunk.get("message", {})
                    parts.append(message.get("content", ""))
                    reasoned = reasoned or bool(message.get("thinking"))
                    if chunk.get("done"):
                        # Usage counts only appear on the final chunk.
                        in_tokens = chunk.get("prompt_eval_count", 0)
                        out_tokens = chunk.get("eval_count", 0)
        except requests.RequestException as exc:
            raise ConversationError(f"Ollama call failed: {exc}") from exc

        text = "".join(parts).strip()
        if not text and reasoned:
            raise ConversationError(
                f"{self.model} spent its whole {max_tokens}-token budget reasoning and never "
                "answered — raise conversation.max_tokens or pick a non-reasoning model"
            )
        return Completion(text=text, input_tokens=in_tokens, output_tokens=out_tokens)
