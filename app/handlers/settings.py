"""The settings panel: reading and saving model, TTS and STT settings."""

from __future__ import annotations

import asyncio
import logging

from fastapi import WebSocket

from ..providers import REGISTRY as PROVIDERS
from ..providers.base import DEFAULT_PROVIDER, Capability, ConversationError
from ..services.call_map import call_map_status
from ..services.changeset import changeset_status
from ..services.diff_service import get_head_sha
from ..services.project_overview import overview_status
from ..services.settings_store import (
    apply_overrides,
    effective_harness_settings,
    effective_settings,
    effective_stt_settings,
    effective_tts_settings,
    load_overrides,
    sanitize,
    sanitize_harness,
    sanitize_stt,
    sanitize_tts,
    save_overrides,
)
from ..services.voice_service import STTClient, TTSClient
from ..web import runtime
from ..web.config import CONFIG
from ..web.runtime import send_error, send_json
from ..web.session import Session
from .registry import handler

log = logging.getLogger("ai_pear_review")


@handler("get_settings")
async def handle_get_settings(ws: WebSocket, session: Session, payload: dict) -> None:
    """Sends the settings panel everything it needs to draw itself.

    That is the model settings in force, the models the configured provider
    reports (when it can list them — Capability.MODEL_LISTING; Ollama
    filters out non-chat models), and the freshness of the .context/ prep
    files. Offering models as choices saves the reviewer typing a model name
    exactly right.

    The client asks for this rather than getting it on connect: the panel is
    usually closed, and listing models means an HTTP call to another service
    that has no business in the connection handshake.

    A missing prep file is reported here and nowhere else. Narration runs
    fine without one, so a notice would be noise on every session for a
    benefit the reviewer may not want — but it answers the question when
    someone wonders why narration seems thin.

    A stale prep file is a different matter, and does get a notice at review
    start (see _warn_about_stale_prep in review_flow.py). Measured on this
    repo: a call map written before a refactor listed a module that had been
    deleted and knew nothing of the handlers package that replaced it, and
    the app fed that to the model as fact."""
    settings = effective_settings(CONFIG["conversation"])
    installed: list[str] = []
    # Optional "provider": whose models to list. Mid-switch the panel is
    # showing a provider it hasn't saved yet (see suppressProviderSync in
    # static/js/settings.js), and without this the choices would be the saved provider's —
    # pick Anthropic and the list is still Ollama's installed models, with
    # no Claude model selectable at all. Validated against the registry
    # rather than trusted, since this arrives over an unauthenticated
    # socket; an unknown or missing value falls back to the saved provider.
    asked = payload.get("provider")
    chosen = asked if isinstance(asked, str) and asked in PROVIDERS else settings.get("provider")
    provider_cls = PROVIDERS.get(chosen or DEFAULT_PROVIDER)
    if provider_cls is not None and provider_cls.capabilities & Capability.MODEL_LISTING:
        try:
            installed = await asyncio.to_thread(provider_cls.list_models, settings.get(provider_cls.name) or {})
        except ConversationError as exc:  # nothing here is worth failing the panel over
            log.info("Could not list %s models: %s", provider_cls.name, exc)
    await send_json(
        ws,
        "settings",
        {
            "settings": settings,
            "installed_models": installed,
            "tts_settings": effective_tts_settings(CONFIG["tts"]),
            "stt_settings": effective_stt_settings(CONFIG["stt"]),
            "harness_settings": effective_harness_settings(CONFIG["harness"]),
            "context_status": await asyncio.to_thread(context_status, CONFIG["server"].get("repo_path", ".")),
        },
    )


def context_status(repo_path: str) -> dict:
    """Reports how fresh each .context/ prep file is, and the command that
    regenerates it.

    The UI shows that command rather than offering a button, because this
    app never invokes a CLI agent itself (see briefing_service.py).

    Runs off-thread: it shells out to git for HEAD. Also called by
    review_flow at review start, to warn when a prep file has drifted."""
    head = get_head_sha(repo_path)
    return {
        "project_overview": {
            **overview_status(repo_path, head),
            "refresh_hint": "Ask Claude Code: use the project-overview skill",
        },
        "call_map": {
            **call_map_status(repo_path, head),
            "refresh_hint": "Ask Claude Code: use the call-map skill",
        },
        "changeset": {
            **changeset_status(repo_path, head),
            "refresh_hint": "Ask Claude Code: use the prep-review skill",
        },
    }


@handler("set_settings")
async def handle_set_settings(ws: WebSocket, session: Session, payload: dict) -> None:
    """Saves settings from the UI and applies what can be applied now.

    Payload: {"settings": {...}}. Everything is sanitised first (see
    settings_store's allowlist): this arrives over an unauthenticated
    socket, so it must not be able to reach server.host, repo_path or the
    debug capture flag. What survives is layered onto the live CONFIG and
    persisted for the next launch.

    Conversation settings take effect on the next connection, not this one.
    This session's ConversationClient was built at connect time with the old
    values, and swapping it mid-conversation would leave the in-flight
    history pointing at a different model. The reply says so plainly, which
    beats pretending it applied and leaving the reviewer wondering why
    nothing changed.

    TTS and STT settings apply immediately, this connection included. They
    are singletons built once at startup (see app/web/runtime.py), never per
    connection, so "reconnect to pick it up" would simply be untrue of them,
    and there is no conversation history for a swap to orphan. Reassigning
    runtime.TTS and runtime.STT is enough: every call site resolves
    `runtime.TTS.synthesize` at the moment it runs (see try_speak), so a
    call already in flight keeps the object it started with."""
    clean = sanitize(payload.get("settings") or {})
    clean_tts = sanitize_tts(payload.get("settings") or {})
    clean_stt = sanitize_stt(payload.get("settings") or {})
    clean_harness = sanitize_harness(payload.get("settings") or {})
    if not clean and not clean_tts and not clean_stt and not clean_harness:
        await send_error(ws, "No recognised settings to save.")
        return

    stored = apply_overrides(load_overrides(session.repo_path), {**clean, **clean_tts, **clean_stt, **clean_harness})
    save_overrides(session.repo_path, stored)

    if clean:
        CONFIG["conversation"] = apply_overrides(CONFIG["conversation"], clean)
    if clean_tts:
        CONFIG["tts"] = {**CONFIG["tts"], **clean_tts["tts"]}
        runtime.TTS = TTSClient(CONFIG["tts"])
    if clean_stt:
        CONFIG["stt"] = {**CONFIG["stt"], **clean_stt["stt"]}
        runtime.STT = STTClient(CONFIG["stt"])
    if clean_harness:
        # Like tts/stt, applies now: each Act Now starts its own agent process,
        # so there's no live connection to orphan.
        CONFIG["harness"] = {**CONFIG["harness"], **clean_harness["harness"]}
        await send_json(ws, "service_status", {"act_now": runtime.act_now_status()})

    await send_json(
        ws,
        "settings",
        {
            "settings": effective_settings(CONFIG["conversation"]),
            "tts_settings": effective_tts_settings(CONFIG["tts"]),
            "stt_settings": effective_stt_settings(CONFIG["stt"]),
            "harness_settings": effective_harness_settings(CONFIG["harness"]),
            "saved": True,
            "applies_on_reconnect": bool(clean),
            "tts_applied_now": bool(clean_tts),
            "stt_applied_now": bool(clean_stt),
            "harness_applied_now": bool(clean_harness),
        },
    )
