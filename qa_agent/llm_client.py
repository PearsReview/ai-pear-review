"""Standalone Ollama HTTP client for qa_agent's own judging/driving needs.

Deliberately NOT a call into app/services/conversation_service.py's
ConversationClient — see the isolation section of the plan this was
built from. This is a completely separate implementation making a
brand-new HTTP request per call, so it can never share "conversation
session" state with whatever the app-under-test's own ConversationClient
is doing for the browser tab qa_agent happens to be driving.

DEFAULT_BASE_URL/DEFAULT_MODEL below are only a fallback for anyone
constructing LLMClient() directly (ad hoc debugging, a script outside
the pytest suite) — every real test constructs it via conftest.py's
judge_model_config fixture instead, which reads the real
app/config.yaml's conversation.ollama.model/base_url at test-collection
time so the judge always targets whatever model the app-under-test is
actually configured to run, not a constant that silently drifts out of
sync with it (this file's own DEFAULT_MODEL used to read
"llama3.2:3b-instruct-q4_K_M" while config.yaml had long since moved to
"qwen2.5-coder:7b-instruct-q4_K_M" — a judge silently judging with the
wrong model is exactly the kind of thing this suite exists to catch in
the app, so it can't be allowed to happen to the suite itself).

Known gap, not fixed here: Ollama-only, no Anthropic-provider branch,
even though app/config.yaml's conversation.provider can be "anthropic".
Deliberate for now — the judge's whole value is being a genuinely
independent second opinion, and hardcoding a separate Ollama model is
arguably a *stronger* independence guarantee than mirroring whatever
provider the app-under-test happens to be configured for would be. Flag
this explicitly for whoever adapts this suite to an Anthropic-configured
target app later, rather than leaving it unstated.
"""

from __future__ import annotations

import json
import re

import requests

DEFAULT_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "qwen2.5-coder:7b-instruct-q4_K_M"  # fallback only — see module docstring; real tests get this from conftest.py's judge_model_config fixture instead
_VALID_VERDICTS = ("yes", "no", "unsure")


class LLMClient:
    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        model: str = DEFAULT_MODEL,
        timeout: float = 60.0,
        num_ctx: int | None = None,
    ):
        self.base_url = base_url
        self.model = model
        self.timeout = timeout
        # Must match whatever the app-under-test sends, and for the same
        # reason the model name must: not correctness, but speed. num_ctx
        # is a LOAD-time parameter in Ollama, so two clients asking the
        # same model for different context sizes make it reload between
        # every call. Measured here on a trivial 5-token prompt: ~0.8s per
        # call when both sides agree, ~7-9s when they alternate — a ~9x
        # penalty that is pure reload overhead, nothing to do with the
        # prompt. The suite alternates constantly (one app call, then k
        # judge votes, then the next app call), so a mismatch is worst-case
        # for exactly this pattern: it added ~6 minutes to a full run and
        # pushed several already-marginal timeouts over the edge.
        self.num_ctx = num_ctx

    def _generate(self, system_prompt: str, user_prompt: str) -> str:
        payload = {"model": self.model, "system": system_prompt, "prompt": user_prompt, "stream": False}
        if self.num_ctx:
            payload["options"] = {"num_ctx": self.num_ctx}
        resp = requests.post(f"{self.base_url}/api/generate", json=payload, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json().get("response", "")

    def judge(self, system_prompt: str, user_prompt: str) -> dict:
        """Calls the model and parses a {"verdict", "reason"} object from
        its response. Never raises — any failure (network, bad JSON,
        missing/unrecognized fields) becomes an "unsure" verdict instead,
        so a single flaky call can never crash a test (see the plan's
        "tests always pass, verdicts get recorded" design)."""
        try:
            raw = self._generate(system_prompt, user_prompt)
        except Exception as exc:
            return {"verdict": "unsure", "reason": f"LLM call failed: {exc}"}
        return _parse_json_object(raw, required_keys=("verdict", "reason"), verdict_field="verdict")

    def next_action(self, system_prompt: str, user_prompt: str) -> dict | None:
        """Like judge(), but for Stage 3's driving loop: parses a
        next-action JSON object with no fixed key schema, and returns
        None (rather than a fallback dict) on failure — the driving loop
        needs to distinguish "no valid action at all" from "the model
        chose some particular action", which a judge()-style fallback
        object would blur."""
        try:
            raw = self._generate(system_prompt, user_prompt)
        except Exception:
            return None
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) and "action" in data else None


def _parse_json_object(raw: str, required_keys: tuple[str, ...], verdict_field: str) -> dict:
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return {"verdict": "unsure", "reason": "could not parse model output"}
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {"verdict": "unsure", "reason": "could not parse model output"}
    if not isinstance(data, dict):
        return {"verdict": "unsure", "reason": "model output was not a JSON object"}
    verdict = str(data.get(verdict_field, "")).strip().lower()
    if verdict not in _VALID_VERDICTS:
        return {"verdict": "unsure", "reason": "model gave an unrecognized verdict"}
    reason = str(data.get("reason", "")).strip()
    return {"verdict": verdict, "reason": reason}
