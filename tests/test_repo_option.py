"""run.py --repo: reviewing a repo other than the current directory.

The plumbing is indirect (a CLI flag becomes an environment variable that
app/web/config.py reads at import) because CONFIG is resolved once, at
import, before any argument could be passed in. Nothing else in the app
reads REVIEW_REPO_PATH, so these tests are what keep the two ends agreeing.

CONFIG's import-time resolution is also why the config half runs in a
subprocess: importing it again in this process would not re-read the
environment, and reloading it would hand the rest of the suite a different
CONFIG object than the one its modules already hold.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from run import REPO_PATH_ENV, parse_args

REPO_ROOT = Path(__file__).resolve().parent.parent

_PRINT_REPO_PATH = "from app.web.config import CONFIG; print(CONFIG['server']['repo_path'])"


def _repo_path_seen_by_config(env_value: str | None) -> str:
    env = {**os.environ}
    env.pop(REPO_PATH_ENV, None)
    if env_value is not None:
        env[REPO_PATH_ENV] = env_value
    result = subprocess.run(
        [sys.executable, "-c", _PRINT_REPO_PATH],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_no_flag_means_the_current_directory():
    assert parse_args([]).repo is None
    assert _repo_path_seen_by_config(None) == "."


def test_the_flag_is_what_config_reviews(tmp_path: Path):
    assert _repo_path_seen_by_config(str(tmp_path)) == str(tmp_path)


def test_parse_args_accepts_a_path():
    assert parse_args(["--repo", "/somewhere/else"]).repo == "/somewhere/else"


def test_an_unknown_flag_is_rejected():
    with pytest.raises(SystemExit):
        parse_args(["--reviewing", "/somewhere/else"])


def test_a_path_that_is_not_a_directory_stops_startup(tmp_path: Path):
    """Preflight would otherwise report a confusing 'not a git repository'
    for what is really a typo in the path."""
    missing = tmp_path / "typo"
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "run.py"), "--repo", str(missing)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "is not a directory" in (result.stdout + result.stderr)
