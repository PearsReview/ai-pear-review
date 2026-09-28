"""settings_store.py's tts/stt-specific additions - sanitize_tts/
sanitize_stt, effective_tts_settings/effective_stt_settings, and
apply_overrides' generic pass-through merging "tts"/"stt" one level deep
without cross-contaminating the conversation-shaped keys or each other.

The property that matters most here is the same one the original
sanitize()/ALLOWED_* allowlist exists for: this arrives over an
unauthenticated WebSocket, so anything not explicitly allowed must be
dropped, not merged through. A second property matters just as much now
that each of tts/stt carries two fields (endpoint + token): saving one
must never silently wipe the other - see the merge-not-replace tests
below, which pin the bug a plain "wholesale replace" would reintroduce.
"""

from __future__ import annotations

from app.services.settings_store import (
    apply_overrides,
    effective_stt_settings,
    effective_tts_settings,
    sanitize,
    sanitize_stt,
    sanitize_tts,
)


def test_sanitize_tts_keeps_endpoint_and_token():
    clean = sanitize_tts({"tts": {"endpoint": "http://example:9000/speech", "token": "s3cr3t", "method": "GET"}})
    assert clean == {"tts": {"endpoint": "http://example:9000/speech", "token": "s3cr3t"}}


def test_sanitize_tts_keeps_only_the_endpoint_when_no_token_given():
    clean = sanitize_tts({"tts": {"endpoint": "http://example:9000/speech"}})
    assert clean == {"tts": {"endpoint": "http://example:9000/speech"}}


def test_sanitize_tts_drops_everything_not_endpoint_or_token():
    """method/request_format/mime_type describe the wire protocol of the
    one tested backend - letting a client change them would just break
    synthesis, not "configure" anything."""
    clean = sanitize_tts({"tts": {"method": "GET", "request_format": "multipart", "timeout_seconds": 5}})
    assert clean == {}


def test_sanitize_tts_ignores_a_missing_or_malformed_tts_section():
    assert sanitize_tts({}) == {}
    assert sanitize_tts({"tts": "not a dict"}) == {}
    assert sanitize_tts({"tts": None}) == {}
    assert sanitize_tts({"conversation": {"provider": "anthropic"}}) == {}


def test_sanitize_tts_ignores_an_attempt_to_smuggle_other_keys():
    """The allowlist is what matters, not "is this a dict shaped like
    tts:" - server.host/repo_path-style keys must never ride through."""
    clean = sanitize_tts({"tts": {"endpoint": "http://x/y", "host": "0.0.0.0", "capture_llm_calls": True}})
    assert clean == {"tts": {"endpoint": "http://x/y"}}


def test_sanitize_stt_keeps_endpoint_and_token():
    """stt is sanitize_tts' mirror - same allowlist, same shape, same
    reasoning, just scoped to the stt: block."""
    clean = sanitize_stt(
        {"stt": {"endpoint": "http://example:9000/transcribe", "token": "s3cr3t", "response_field": "text"}}
    )
    assert clean == {"stt": {"endpoint": "http://example:9000/transcribe", "token": "s3cr3t"}}


def test_sanitize_stt_ignores_a_missing_or_malformed_stt_section():
    assert sanitize_stt({}) == {}
    assert sanitize_stt({"stt": "not a dict"}) == {}
    assert sanitize_stt({"stt": None}) == {}


def test_sanitize_stt_does_not_leak_into_sanitize_tts_or_vice_versa():
    """A payload carrying both tts and stt sections must keep them
    separate - a shared allowlist implementation is exactly the kind of
    thing that could accidentally cross-wire the two."""
    payload = {
        "tts": {"endpoint": "http://tts-host/speech", "token": "tts-token"},
        "stt": {"endpoint": "http://stt-host/transcribe", "token": "stt-token"},
    }
    assert sanitize_tts(payload) == {"tts": {"endpoint": "http://tts-host/speech", "token": "tts-token"}}
    assert sanitize_stt(payload) == {"stt": {"endpoint": "http://stt-host/transcribe", "token": "stt-token"}}


def test_effective_tts_settings_reads_the_endpoint_and_reports_token_set_without_leaking_it():
    """token itself must never round-trip back to the client - same reason
    effective_settings() never returns anthropic's api_key. token_set is
    enough for the UI to say "a token is saved" without repeating it."""
    assert effective_tts_settings({"endpoint": "http://x/y", "token": "s3cr3t"}) == {
        "endpoint": "http://x/y",
        "token_set": True,
    }
    assert effective_tts_settings({"endpoint": "http://x/y"}) == {"endpoint": "http://x/y", "token_set": False}


def test_effective_tts_settings_handles_a_missing_endpoint():
    assert effective_tts_settings({}) == {"endpoint": None, "token_set": False}


def test_effective_stt_settings_reads_the_endpoint_and_reports_token_set_without_leaking_it():
    assert effective_stt_settings({"endpoint": "http://x/y", "token": "s3cr3t"}) == {
        "endpoint": "http://x/y",
        "token_set": True,
    }
    assert effective_stt_settings({}) == {"endpoint": None, "token_set": False}


def test_apply_overrides_merges_tts_one_level_deep_instead_of_replacing_it():
    """The bug this pins: saving just a new endpoint must not silently
    drop a previously-saved token. Before tts carried more than one field
    this was safe to implement as a plain replace (see git history) - now
    it must merge one level deep, same as the ollama/anthropic branch."""
    stored = {
        "provider": "ollama",
        "ollama": {"model": "old-model"},
        "tts": {"endpoint": "http://old", "token": "keep-me"},
    }
    merged = apply_overrides(stored, {"tts": {"endpoint": "http://new"}})
    assert merged["tts"] == {"endpoint": "http://new", "token": "keep-me"}
    assert merged["provider"] == "ollama"
    assert merged["ollama"] == {"model": "old-model"}, "an unrelated key must survive untouched"


def test_apply_overrides_merges_a_new_tts_token_without_touching_the_endpoint():
    stored = {"tts": {"endpoint": "http://keep-me"}}
    merged = apply_overrides(stored, {"tts": {"token": "new-token"}})
    assert merged["tts"] == {"endpoint": "http://keep-me", "token": "new-token"}


def test_apply_overrides_merges_stt_one_level_deep_too():
    stored = {"stt": {"endpoint": "http://old", "token": "keep-me"}}
    merged = apply_overrides(stored, {"stt": {"endpoint": "http://new"}})
    assert merged["stt"] == {"endpoint": "http://new", "token": "keep-me"}


def test_a_tts_key_in_the_overrides_file_does_not_leak_into_sanitize():
    """sanitize() (conversation-only) must never accidentally pick up a
    "tts"/"stt" key sitting in the same raw payload/overrides dict - the
    two are kept as separate sanitize calls in handle_set_settings specifically so
    this can't happen; this pins that sanitize() itself never emits
    "tts"/"stt"."""
    clean = sanitize(
        {"provider": "ollama", "tts": {"endpoint": "http://sneaky"}, "stt": {"endpoint": "http://also-sneaky"}}
    )
    assert "tts" not in clean
    assert "stt" not in clean
