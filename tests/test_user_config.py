"""The user-level config overlay — ~/.config/pear-review/config.yaml (or the
file REVIEW_USER_CONFIG names) deep-merged over app/config.yaml.

load_config() is called directly here rather than re-importing
app.web.config: CONFIG is resolved once at import, but load_config() reads the
path on every call, so pointing REVIEW_USER_CONFIG at a tmp file is enough.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.web.config import USER_CONFIG_ENV, _deep_merge, load_config


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def user_config(tmp_path: Path, monkeypatch) -> Path:
    path = tmp_path / "config.yaml"
    monkeypatch.setenv(USER_CONFIG_ENV, str(path))
    return path


def test_deep_merge_overrides_leaves_and_keeps_siblings():
    base = {"conversation": {"provider": "ollama", "ollama": {"model": "a", "num_ctx": 8192}}, "server": {"port": 1}}
    merged = _deep_merge(base, {"conversation": {"ollama": {"model": "b"}}})
    assert merged == {
        "conversation": {"provider": "ollama", "ollama": {"model": "b", "num_ctx": 8192}},
        "server": {"port": 1},
    }


def test_deep_merge_does_not_mutate_its_inputs():
    base = {"a": {"b": 1}}
    override = {"a": {"c": 2}}
    _deep_merge(base, override)
    assert base == {"a": {"b": 1}}
    assert override == {"a": {"c": 2}}


def test_deep_merge_replaces_a_dict_with_a_scalar_and_vice_versa():
    assert _deep_merge({"a": {"b": 1}}, {"a": 5}) == {"a": 5}
    assert _deep_merge({"a": 5}, {"a": {"b": 1}}) == {"a": {"b": 1}}


def test_no_user_file_means_the_app_defaults(user_config: Path):
    assert not user_config.exists()
    config = load_config()
    assert config["conversation"]["provider"] == "ollama"


def test_user_file_is_merged_over_the_app_defaults(user_config: Path):
    _write(user_config, "conversation:\n  provider: openai\n  openai:\n    model: gpt-5\n")
    config = load_config()
    assert config["conversation"]["provider"] == "openai"
    assert config["conversation"]["openai"]["model"] == "gpt-5"
    # Untouched siblings fall through to app/config.yaml.
    assert config["conversation"]["openai"]["api_key_env"] == "OPENAI_API_KEY"
    assert "ollama" in config["conversation"]


def test_an_empty_user_file_is_ignored(user_config: Path):
    _write(user_config, "")
    assert load_config()["conversation"]["provider"] == "ollama"


def test_a_non_mapping_user_file_is_ignored_with_a_warning(user_config: Path, caplog):
    _write(user_config, "- not\n- a mapping\n")
    with caplog.at_level("WARNING", logger="ai_pear_review"):
        config = load_config()
    assert config["conversation"]["provider"] == "ollama"
    assert "expected a mapping" in caplog.text


def test_the_suite_never_sees_a_developer_s_own_user_config():
    """tests/conftest.py points REVIEW_USER_CONFIG at a file that doesn't
    exist, before app.web.config is first imported."""
    import os

    assert not Path(os.environ[USER_CONFIG_ENV]).exists()
