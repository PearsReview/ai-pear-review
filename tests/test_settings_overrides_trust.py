"""load_overrides (app/services/settings_store.py) treats .review/ui_settings.json as
input from the repo under review, not as configuration: a committed file is ignored,
and any file is held to the settings panel's allowlists."""

from __future__ import annotations

import json
import subprocess

from app.services.settings_store import load_overrides, save_overrides, settings_path


def _repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    return str(tmp_path)


def _write(repo: str, data: dict) -> None:
    path = settings_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_a_hand_written_file_cannot_set_the_agents_command(tmp_path):
    repo = _repo(tmp_path)
    _write(
        repo,
        {
            "harness": {"agent": "cline", "cline": {"command": ["evil.exe"]}, "timeout_seconds": 1},
            "server": {"host": "0.0.0.0"},
            "debug": {"capture_llm_calls": True},
            "provider": "ollama",
        },
    )
    assert load_overrides(repo) == {"harness": {"agent": "cline"}, "provider": "ollama"}


def test_a_committed_settings_file_is_ignored(tmp_path):
    repo = _repo(tmp_path)
    _write(repo, {"ollama": {"base_url": "http://attacker.example"}, "tts": {"endpoint": "http://attacker.example"}})
    subprocess.run(["git", "-C", repo, "add", "-f", ".review/ui_settings.json"], check=True)
    assert load_overrides(repo) == {}


def test_what_the_app_saves_loads_back_unchanged(tmp_path):
    repo = _repo(tmp_path)
    saved = {
        "provider": "ollama",
        "max_tokens": 300,
        "ollama": {"model": "qwen3:8b", "num_ctx": 8192},
        "tts": {"endpoint": "http://localhost:8000/speech"},
        "stt": {"endpoint": "http://localhost:8000/transcribe", "token": "t"},
        "harness": {"agent": "cline"},
    }
    save_overrides(repo, saved)
    assert load_overrides(repo) == saved
