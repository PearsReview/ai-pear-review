"""settings_store.py's handling of the openai provider block — the same
allowlist, merge and echo rules the ollama/anthropic blocks follow.

What matters: this arrives over an unauthenticated WebSocket, so only the
allowlisted keys survive sanitize() (in particular not api_key_env, which
would let a client choose which environment variable the key is read from),
and effective_settings() never echoes a secret back.
"""

from __future__ import annotations

from app.services.settings_store import apply_overrides, effective_settings, sanitize


def test_sanitize_keeps_the_openai_block_s_allowed_keys():
    clean = sanitize(
        {
            "provider": "openai",
            "openai": {"model": "gpt-5", "base_url": "https://gw.example/v1", "max_tokens": 4096},
        }
    )
    assert clean == {
        "provider": "openai",
        "openai": {"model": "gpt-5", "base_url": "https://gw.example/v1", "max_tokens": 4096},
    }


def test_sanitize_drops_api_key_env_and_unknown_openai_keys():
    clean = sanitize({"openai": {"model": "gpt-5", "api_key_env": "HOME", "api_key": "sk-x", "headers": {}}})
    assert clean == {"openai": {"model": "gpt-5"}}


def test_sanitize_drops_a_non_dict_openai_block():
    assert sanitize({"openai": "gpt-5"}) == {}


def test_apply_overrides_merges_openai_one_level_deep():
    stored = {"provider": "ollama", "openai": {"model": "old", "base_url": "https://keep-me/v1"}}
    merged = apply_overrides(stored, {"provider": "openai", "openai": {"model": "new"}})
    assert merged["provider"] == "openai"
    assert merged["openai"] == {"model": "new", "base_url": "https://keep-me/v1"}


def test_effective_settings_reports_the_openai_block_without_secrets():
    effective = effective_settings(
        {
            "provider": "openai",
            "openai": {
                "model": "gpt-5",
                "base_url": "https://gw.example/v1",
                "max_tokens": 2048,
                "api_key_env": "OPENAI_API_KEY",
                "api_key": "sk-should-not-appear",
            },
        }
    )
    assert effective["openai"] == {"model": "gpt-5", "base_url": "https://gw.example/v1", "max_tokens": 2048}
    assert "sk-should-not-appear" not in repr(effective)


def test_effective_settings_tolerates_a_missing_openai_block():
    assert effective_settings({"provider": "ollama"})["openai"] == {
        "model": None,
        "base_url": None,
        "max_tokens": None,
    }
