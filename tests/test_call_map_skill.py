"""Pins the call-map skill's scanner to what the app actually reads.

Same failure mode as test_prep_review_keys.py guards against, for the same
reason: the skill (.claude/skills/call-map/) deliberately does not import
app.*, because it has to run against whatever repo is being reviewed. The
cost of that independence is that the file format is agreed by convention,
and a convention can drift silently — a map written in a shape the app
doesn't recognise is ignored with no error and no warning, and the
reviewer simply never sees caller information without ever learning why.

So these tests run the real scanner over a real throwaway git repo and
then read the result through the app's own loader.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

from app.services.call_map import call_map_prompt_block, call_map_status, load_call_map

_SKILL_DIR = Path(__file__).resolve().parent.parent / ".claude" / "skills" / "call-map"


def _load_scanner():
    spec = importlib.util.spec_from_file_location("scan_calls", _SKILL_DIR / "scan_calls.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "billing.py").write_text(
        "def charge(amount):\n    return validate(amount)\n\ndef validate(amount):\n    return amount\n",
        encoding="utf-8",
    )
    (tmp_path / "flow.py").write_text(
        "from billing import charge\n\ndef checkout(cart):\n    return charge(cart.total)\n",
        encoding="utf-8",
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_billing.py").write_text(
        "from billing import charge\n"
        "\n"
        "def test_charge():\n"
        "    assert charge(1)\n"
        "\n"
        "def test_charge_again():\n"
        "    assert charge(2)\n",
        encoding="utf-8",
    )
    for args in (
        ["init", "-q"],
        ["config", "user.email", "a@b.c"],
        ["config", "user.name", "a"],
        ["add", "-A"],
        ["commit", "-q", "-m", "init"],
    ):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)
    return tmp_path


def _scan_and_write(repo: Path) -> None:
    scanner = _load_scanner()
    subprocess.run(
        ["python", str(_SKILL_DIR / "scan_calls.py"), "--repo", str(repo)],
        check=True,
        capture_output=True,
    )
    assert scanner  # the module also has to be importable on its own


def test_scanner_output_is_readable_by_the_app(repo: Path):
    """The end-to-end contract: scanner writes, app loads, app produces a
    prompt block naming the real caller."""
    _scan_and_write(repo)

    assert load_call_map(str(repo)) is not None
    block = call_map_prompt_block(str(repo), "billing.py", "def charge(amount):\n+    pass")
    assert block is not None, "the app could not use the map the skill just wrote"
    assert "charge is called by:" in block
    assert "checkout (flow.py)" in block


def test_scanner_finds_intra_file_callers(repo: Path):
    _scan_and_write(repo)
    block = call_map_prompt_block(str(repo), "billing.py", "def validate(amount):\n+    pass")
    assert "validate is called by:" in block
    assert "charge (billing.py)" in block


def test_production_callers_rank_above_test_callers(repo: Path):
    """charge() has one production caller and two test callers. Both this
    scanner and the app cap the list, so if tests sorted first they would
    fill the budget and hide the production dependency — which is the one
    the reviewer needs."""
    _scan_and_write(repo)
    data = load_call_map(str(repo))
    charge = next(s for s in data["symbols"] if s["name"] == "charge")
    assert charge["callers"][0]["file"] == "flow.py"
    assert charge["caller_count"] == 3


def test_status_agrees_with_the_scanners_own_status_output(repo: Path):
    _scan_and_write(repo)
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    status = call_map_status(str(repo), head)
    assert status["present"] is True
    assert status["head_moved"] is False
    assert status["symbol_count"] >= 2


def test_ambiguous_names_are_dropped_rather_than_guessed(repo: Path):
    """Two definitions of the same name means a call to it can't be
    attributed. The scanner must drop it — attributing it to the wrong file
    would tell the reviewer that changing this function affects callers
    that in fact call a different function entirely."""
    (repo / "other.py").write_text(
        "def charge(x):\n    return x\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    _scan_and_write(repo)

    data = load_call_map(str(repo))
    assert not [s for s in data["symbols"] if s["name"] == "charge"], (
        "charge is now defined twice and must not be attributed to either definition"
    )
    # validate is still unambiguous and must survive
    assert [s for s in data["symbols"] if s["name"] == "validate"]


def test_refuses_to_write_an_empty_map(tmp_path: Path):
    """A file that exists but says nothing is worse than no file: the
    settings panel would report a call map as present and fresh while the
    reviewer gets nothing from it."""
    (tmp_path / "lonely.py").write_text("x = 1\n", encoding="utf-8")
    for args in (
        ["init", "-q"],
        ["config", "user.email", "a@b.c"],
        ["config", "user.name", "a"],
        ["add", "-A"],
        ["commit", "-q", "-m", "i"],
    ):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    result = subprocess.run(
        ["python", str(_SKILL_DIR / "scan_calls.py"), "--repo", str(tmp_path)],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "refusing to write an empty map" in result.stderr
    assert not (tmp_path / ".context" / "call_map.json").exists()


def test_a_file_that_does_not_parse_is_skipped_not_fatal(repo: Path):
    """A repo mid-edit routinely has one. Refusing to produce a map because
    of it would fail exactly when the map is most wanted."""
    (repo / "broken.py").write_text("def oops(:\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)

    result = subprocess.run(
        ["python", str(_SKILL_DIR / "scan_calls.py"), "--repo", str(repo)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "broken.py" in result.stderr, "the skipped file must be reported, not silently dropped"
    assert load_call_map(str(repo)) is not None


def test_test_coverage_is_counted_before_the_caller_cap(repo: Path, monkeypatch):
    """Callers are sorted production-first and then capped, which is exactly
    what drops test callers — so the coverage fields have to come from the
    full list, not the stored one."""
    scanner = _load_scanner()
    monkeypatch.setattr(scanner, "MAX_CALLERS_STORED", 1)
    symbols, _ = scanner.scan(str(repo))
    charge = next(s for s in symbols if s["name"] == "charge")
    assert [c["file"] for c in charge["callers"]] == ["flow.py"]
    assert charge["test_caller_count"] == 2
    assert charge["test_files"] == ["tests/test_billing.py"]
    validate = next(s for s in symbols if s["name"] == "validate")
    assert validate["test_caller_count"] == 0 and validate["test_files"] == []
