"""User-adjustable model settings, layered over config.yaml.

config.yaml stays the source of truth and is NEVER rewritten by the app.
That's deliberate: it carries a great deal of explanatory comment — why
each provider has its own key, why max_chars is what it is, why capture is
test-only — and a pyyaml round-trip (safe_load then safe_dump) silently
discards every one of those comments. Persisting UI changes into a small
separate JSON file and layering it on top at startup keeps the documented
file hand-editable and the overrides machine-written, with neither
clobbering the other.

Layering is intentionally shallow-per-section (see apply_overrides): an
override for conversation.model must not wipe out conversation.ollama's
sibling keys, and a stored override for a key the user later removed from
config.yaml simply stops mattering.

Lives under .review/ alongside session_state.json — same
already-gitignored, per-repo directory session_store.py established, since
this is per-checkout preference rather than anything to commit.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

from .harness_service import agent_model, research_note

log = logging.getLogger(__name__)

_SETTINGS_DIR = ".review"
_SETTINGS_FILENAME = "ui_settings.json"

# Only these are settable from the UI. An allowlist rather than "merge
# whatever arrives": this file is written from a WebSocket message, and
# without it a client could inject arbitrary config (server.host,
# repo_path, the debug capture flag) into every future run of the app.
# provider/model/base_url pick the endpoint; num_ctx/max_tokens/
# timeout_seconds are the three knobs that actually decide whether a call
# fits and how long it takes.
ALLOWED_CONVERSATION_KEYS = ("provider", "max_tokens", "timeout_seconds")
# max_tokens appears in both lists on purpose: the shared one above is the
# fallback, and a provider block's own value wins (see ConversationClient's
# resolution). Anthropic needs its own because it shares that budget with the
# model's reasoning, so the local model's 300 truncates it.
ALLOWED_PROVIDER_KEYS = ("model", "base_url", "num_ctx", "max_tokens")
# endpoint + token. method/request_format/mime_type still describe the wire
# protocol of the one backend this app is tested against (self-hosted
# kokoro-onnx/faster-whisper services) — they're facts about that protocol,
# not something a user should be turning knobs on; the app would simply
# stop being able to talk to its own configured backend. endpoint and token
# are what genuinely vary per install: a different host/port for a
# self-hosted copy, or a bearer token for a hosted one that requires auth
# (the self-hosted default needs neither method nor a token).
ALLOWED_TTS_KEYS = ("endpoint", "token")
ALLOWED_STT_KEYS = ("endpoint", "token")
# Only the choice of agent — never its command. A command set over this
# socket would be a program this app launches.
ALLOWED_HARNESS_AGENTS = ("none", "cline")

# Top-level override keys that belong to their own config section rather than
# to conversation: (see apply_overrides and app/web/config.py).
SECTION_OVERRIDE_KEYS = ("tts", "stt", "harness")


def settings_path(repo_path: str) -> Path:
    return Path(repo_path) / _SETTINGS_DIR / _SETTINGS_FILENAME


def load_overrides(repo_path: str) -> dict:
    """Whatever the user last saved, or {} — a missing or unreadable file
    is normal (first run, hand-deleted, corrupted by a killed write) and
    must never stop the app from starting: the app simply runs on
    config.yaml's own values, which is exactly the pre-override
    behaviour."""
    path = settings_path(repo_path)
    if not path.exists():
        return {}
    # The file sits inside the repo under review, so a repo can ship one. A file git
    # tracks came with the repo rather than from this machine's settings panel, and is
    # ignored outright: even allowlisted keys (ollama.base_url, the speech endpoints)
    # would send the reviewer's code and voice to a server of the repo's choosing.
    if _is_tracked(repo_path, path):
        log.warning("Ignoring %s: it is committed to the repository, not saved by this app.", path)
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Ignoring unreadable %s: %s", path, exc)
        return {}
    if not isinstance(data, dict):
        return {}
    # The same allowlists the settings panel's own writes pass through, so a file
    # edited by hand (or left by anything else) can't reach keys the panel can't —
    # above all harness.<agent>.command, a program Act Now runs. Everything this app
    # writes passes unchanged.
    return {**sanitize(data), **sanitize_tts(data), **sanitize_stt(data), **sanitize_harness(data)}


def _is_tracked(repo_path: str, path: Path) -> bool:
    """Whether git tracks `path` in `repo_path`. False when git can't say (not a repo,
    no git), which leaves the allowlists in load_overrides as the guard."""
    try:
        result = subprocess.run(
            ["git", "-C", repo_path, "ls-files", "--error-unmatch", "--", str(path)],
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def save_overrides(repo_path: str, overrides: dict) -> None:
    """Writes the whole override set. Best-effort in the same spirit as
    session_store: a preference that fails to persist is worth a log line,
    not a broken review session."""
    path = settings_path(repo_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(overrides, indent=2), encoding="utf-8")
    except OSError as exc:
        log.warning("Could not save settings to %s: %s", path, exc)


def sanitize(incoming: dict) -> dict:
    """Filters a client-supplied settings payload down to the allowlists
    above, dropping anything unrecognised rather than trusting it.

    Shape out is {"provider": str, "max_tokens": int, "timeout_seconds":
    int, "<provider>": {"model": str, "base_url": str, "num_ctx": int}} —
    provider-specific keys nested under the provider they belong to,
    mirroring config.yaml's own structure so a flat "model" can never be
    sent to the wrong API (the footgun config.yaml's `conversation`
    comment describes)."""
    clean: dict = {}
    for key in ALLOWED_CONVERSATION_KEYS:
        if key in incoming:
            clean[key] = incoming[key]
    for provider in ("ollama", "anthropic"):
        section = incoming.get(provider)
        if isinstance(section, dict):
            kept = {k: section[k] for k in ALLOWED_PROVIDER_KEYS if k in section}
            if kept:
                clean[provider] = kept
    return clean


def sanitize_tts(incoming: dict) -> dict:
    """Same allowlist reasoning as sanitize() above, scoped to config.yaml's
    tts: block instead of conversation:. A separate function rather than
    folding into sanitize(): the two blocks don't share a shape (no
    provider concept for tts — it's just a flat endpoint+token), so forcing
    them through the same provider-nesting logic would be more contortion
    than the ~8 lines this saves. Returns {"tts": {...}} (or {}) so the
    caller can merge it straight into the same payload sanitize() already
    produces — "tts" is not a key sanitize() itself ever emits, so there's
    no collision either way.

    A missing or empty token does not mean "clear the token". The client
    only sends "token" when the reviewer has typed a new one, and never
    pre-fills the field with the real secret — so a blank value is
    indistinguishable from "unchanged" by the time it arrives here.
    apply_overrides merges one level into the stored tts dict rather than
    replacing it, so saving only a new endpoint leaves the stored token
    alone."""
    section = incoming.get("tts")
    if isinstance(section, dict):
        kept = {k: section[k] for k in ALLOWED_TTS_KEYS if k in section}
        if kept:
            return {"tts": kept}
    return {}


def sanitize_stt(incoming: dict) -> dict:
    """STT's mirror of sanitize_tts() above — same shape, same reasoning,
    same "blank token means unchanged" contract."""
    section = incoming.get("stt")
    if isinstance(section, dict):
        kept = {k: section[k] for k in ALLOWED_STT_KEYS if k in section}
        if kept:
            return {"stt": kept}
    return {}


def sanitize_harness(incoming: dict) -> dict:
    """{"harness": {"agent": ...}} when the agent is one this app supports, else {}."""
    section = incoming.get("harness")
    if isinstance(section, dict) and section.get("agent") in ALLOWED_HARNESS_AGENTS:
        return {"harness": {"agent": section["agent"]}}
    return {}


def effective_harness_settings(harness_config: dict) -> dict:
    """Reports the harness settings for the panel.

    The agent is settable; the model is read-only, because it comes from the
    reviewer's own Cline settings, or a config.yaml pin — see
    harness_service.resolve_agent_env. Never includes the key."""
    agent = harness_config.get("agent") or "none"
    model = agent_model(harness_config)
    return {
        "agent": agent,
        "agents": list(ALLOWED_HARNESS_AGENTS),
        "provider": model.provider,
        "model": model.model,
        "model_source": model.source,
        "research_note": research_note(),
    }


def effective_tts_settings(tts_config: dict) -> dict:
    """Mirrors effective_settings() below, scoped to tts:. token itself is
    never returned — same reason effective_settings() below never returns
    anthropic's api_key: a secret shouldn't round-trip back over the wire
    just because the UI wants to render its own field. token_set is enough
    for the panel to show "a token is saved" without repeating it."""
    return {"endpoint": tts_config.get("endpoint"), "token_set": bool(tts_config.get("token"))}


def effective_stt_settings(stt_config: dict) -> dict:
    """STT's mirror of effective_tts_settings() above."""
    return {"endpoint": stt_config.get("endpoint"), "token_set": bool(stt_config.get("token"))}


def apply_overrides(conversation_config: dict, overrides: dict) -> dict:
    """Returns conversation_config with overrides layered on top.

    Merges one level into each provider section rather than replacing it,
    so overriding just `model` leaves that provider's `base_url` (and
    anything else config.yaml set) intact. Never mutates its input — the
    caller decides whether to swap it in.

    Also used to merge the overrides file's own contents: app/web/config.py
    passes load_overrides(...) straight in as `conversation_config`. That
    dict can carry top-level "tts" and "stt" keys alongside the
    conversation-shaped ones, and those merge one level deep as well. Each
    holds more than one field (endpoint and token), so replacing outright
    would let a saved endpoint silently drop the stored token — the
    "unchanged means keep it" contract sanitize_tts and sanitize_stt
    describe.

    Never pass a raw overrides dict as the first argument when the target
    is CONFIG["conversation"] specifically. The tts/stt merges in
    app/web/config.py's startup block and in handle_set_settings filter
    those keys out first, for that reason."""
    merged = {**conversation_config}
    for key, value in overrides.items():
        if key in ("ollama", "anthropic", *SECTION_OVERRIDE_KEYS) and isinstance(value, dict):
            merged[key] = {**merged.get(key, {}), **value}
        else:
            merged[key] = value
    return merged


def effective_settings(conversation_config: dict) -> dict:
    """The values currently in force, for the UI to render. Reads straight
    off the live (already-overridden) config so what's shown is what the
    next connection will actually use, not what was last saved."""
    return {
        "provider": conversation_config.get("provider"),
        "max_tokens": conversation_config.get("max_tokens"),
        "timeout_seconds": conversation_config.get("timeout_seconds"),
        "ollama": {
            "model": conversation_config.get("ollama", {}).get("model"),
            "base_url": conversation_config.get("ollama", {}).get("base_url"),
            "num_ctx": conversation_config.get("ollama", {}).get("num_ctx"),
        },
        "anthropic": {
            "model": conversation_config.get("anthropic", {}).get("model"),
            "max_tokens": conversation_config.get("anthropic", {}).get("max_tokens"),
        },
    }
