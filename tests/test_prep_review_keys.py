"""Pins the prep-review skill's hunk scanner to the app's own diff logic.

The skill (.claude/skills/prep-review/) deliberately does NOT import
app.* — it has to run against whatever repo is being reviewed, which
usually isn't this project. The cost of that independence is a copy of
diff_service's hunk-splitting and briefing_service's key derivation, and
a copy can drift.

Drift here is silent and total: a briefing written under a key the app
doesn't look up, or carrying a content_hash that doesn't match the hunk
byte-for-byte, is simply never read (see BriefingClient.load_cached) — no error, no
warning, the reviewer just never sees a briefing and has no way to tell
why. This test is what makes that failure loud instead.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

from app.services.briefing_service import _hunk_content_hash, _pregenerated_briefing_path
from app.services.diff_service import get_review_hunks

_SKILL_DIR = Path(__file__).resolve().parent.parent / ".claude" / "skills" / "prep-review"


def _load_scanner():
    spec = importlib.util.spec_from_file_location("scan_hunks", _SKILL_DIR / "scan_hunks.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repo exercising every shape the two implementations must agree on:
    a multi-hunk tracked file, a file in a subdirectory (path separators get
    flattened into the cache key), and an untracked file (reviewed as one
    whole-file 'all added' hunk, appended after the tracked ones)."""
    repo = tmp_path / "scratch"
    (repo / "pkg").mkdir(parents=True)
    (repo / ".gitignore").write_text(".briefing/\n", encoding="utf-8")
    # Two edits far enough apart that git emits two separate hunks.
    (repo / "wide.py").write_text(
        "def a():\n    return 1\n"
        + "\n\n".join(f"def spacer{i}():\n    return {i}" for i in range(6))
        + "\ndef z():\n    return 26\n",
        encoding="utf-8",
    )
    (repo / "pkg" / "nested.py").write_text("def nested():\n    return 'x'\n", encoding="utf-8")
    _run_git(repo, "init", "-q")
    _run_git(repo, "config", "user.email", "t@e.st")
    _run_git(repo, "config", "user.name", "T")
    _run_git(repo, "add", "-A")
    _run_git(repo, "commit", "-q", "-m", "init")

    text = (repo / "wide.py").read_text(encoding="utf-8")
    text = text.replace("def a():\n    return 1", "def a():\n    # changed at the top\n    return 100")
    text = text.replace("def z():\n    return 26", "def z():\n    # changed at the bottom\n    return 260")
    (repo / "wide.py").write_text(text, encoding="utf-8")
    (repo / "pkg" / "nested.py").write_text("def nested():\n    return 'changed'\n", encoding="utf-8")
    (repo / "brand_new.py").write_text("def fresh():\n    return True\n", encoding="utf-8")
    return repo


def test_scanner_finds_the_same_hunks_as_the_app(repo: Path):
    scanner = _load_scanner()
    app_hunks = get_review_hunks(str(repo))
    skill_hunks = scanner.scan(str(repo))

    assert [h.file_path for h in app_hunks] == [h["file_path"] for h in skill_hunks]
    assert [h.header for h in app_hunks] == [h["header"] for h in skill_hunks]
    # More than one file, more than one hunk in one of them, plus the
    # untracked one — otherwise this passes trivially.
    assert len(app_hunks) >= 4
    assert any(h.file_path == "brand_new.py" for h in app_hunks), "untracked file should be reviewed too"


def test_content_hashes_match_byte_for_byte(repo: Path):
    scanner = _load_scanner()
    app_hunks = get_review_hunks(str(repo))
    skill_hunks = scanner.scan(str(repo))

    for app_hunk, skill_hunk in zip(app_hunks, skill_hunks, strict=True):
        assert skill_hunk["diff"] == app_hunk.diff_context, f"diff text differs for {app_hunk.file_path}"
        assert skill_hunk["content_hash"] == _hunk_content_hash(app_hunk.diff_context)


def test_cache_paths_match(repo: Path):
    scanner = _load_scanner()
    app_hunks = get_review_hunks(str(repo))
    skill_hunks = scanner.scan(str(repo))

    for app_hunk, skill_hunk in zip(app_hunks, skill_hunks, strict=True):
        assert Path(skill_hunk["briefing_path"]) == _pregenerated_briefing_path(str(repo), app_hunk)


def test_a_written_briefing_is_actually_loaded_by_the_app(repo: Path):
    """End to end: the skill writes, the app reads. This is the property
    that actually matters — the three tests above are diagnostics for when
    this one fails."""
    scanner = _load_scanner()
    spec = importlib.util.spec_from_file_location("write_briefing", _SKILL_DIR / "write_briefing.py")
    writer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(writer)

    target = scanner.scan(str(repo))[0]
    path = writer.briefing_path(str(repo), target["file_path"], target["header"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        __import__("json").dumps(
            {
                "file_path": target["file_path"],
                "header": target["header"],
                "content_hash": target["content_hash"],
                "intent": "Written by the prep-review skill.",
                "alternatives_considered": None,
                "risk_notes": None,
                "confidence": "high",
                "source": "prep-review-skill",
            }
        ),
        encoding="utf-8",
    )

    from app.services.briefing_service import BriefingClient

    app_hunk = get_review_hunks(str(repo))[0]
    # conversation=None proves it came from cache: on a miss this returns
    # Briefing.unavailable() instead of calling anything.
    briefing = BriefingClient({}, str(repo)).analyze_hunk(app_hunk, conversation=None)
    assert briefing.intent == "Written by the prep-review skill."
    assert briefing.confidence == "high"


def _find(hunks: list[dict], file_path: str) -> dict:
    match = [h for h in hunks if h["file_path"] == file_path]
    assert match, f"expected a hunk for {file_path}, got {[h['file_path'] for h in hunks]}"
    return match[0]


def test_scan_reports_state_transitions(repo: Path):
    """missing -> fresh once written -> stale once the code moves under it.
    That's the signal the skill uses to know what still needs work, and
    the same check the app makes before trusting a cached briefing.

    The edit below deliberately keeps the file's line COUNT the same. A
    same-size edit leaves the @@ header identical, so the cache key is
    unchanged and the entry goes "stale". An edit that adds or removes
    lines shifts the header, which changes the key — the old entry is then
    orphaned rather than stale, and the hunk reads as "missing". Both are
    correct; they're just different paths, and conflating them is how a
    scanner ends up under-reporting work."""
    scanner = _load_scanner()
    spec = importlib.util.spec_from_file_location("write_briefing", _SKILL_DIR / "write_briefing.py")
    writer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(writer)

    target = _find(scanner.scan(str(repo)), "pkg/nested.py")
    assert target["state"] == "missing"

    path = writer.briefing_path(str(repo), target["file_path"], target["header"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        __import__("json").dumps({"content_hash": target["content_hash"], "intent": "recorded", "confidence": "high"}),
        encoding="utf-8",
    )
    assert _find(scanner.scan(str(repo)), "pkg/nested.py")["state"] == "fresh"

    (repo / "pkg" / "nested.py").write_text("def nested():\n    return 'edited'\n", encoding="utf-8")
    restaled = _find(scanner.scan(str(repo)), "pkg/nested.py")
    assert restaled["header"] == target["header"], "same line count should leave the header (and key) alone"
    assert restaled["state"] == "stale"


# --- summary / kind / theme / related, and change-set themes ----------------

import json  # noqa: E402
import sys  # noqa: E402


def _run_skill(script: str, *args: str) -> subprocess.CompletedProcess:
    # UTF-8 mode: the scripts' messages contain non-ASCII, and a Windows
    # child otherwise writes stderr in the console code page.
    return subprocess.run(
        [sys.executable, "-X", "utf8", str(_SKILL_DIR / script), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def test_new_fields_written_by_the_skill_load_in_the_app(repo: Path):
    scanner = _load_scanner()
    hunks = scanner.scan(str(repo))
    target, other = hunks[0], hunks[-1]
    theme = _run_skill(
        "write_theme.py",
        "--repo",
        str(repo),
        "--id",
        "clamp",
        "--title",
        "Clamp values",
        "--why",
        "Zero broke the printer.",
        "--source",
        "author-session",
    )
    assert theme.returncode == 0, theme.stderr

    related = json.dumps(
        [{"file_path": other["file_path"], "header": other["header"], "relation": "caller_of", "note": "n"}]
    )
    result = _run_skill(
        "write_briefing.py",
        "--repo",
        str(repo),
        "--file-path",
        target["file_path"],
        "--header",
        target["header"],
        "--content-hash",
        target["content_hash"],
        "--intent",
        "why",
        "--summary",
        "what",
        "--kind",
        "move",
        "--theme",
        "clamp",
        "--related",
        related,
    )
    assert result.returncode == 0, result.stderr
    assert "warning" not in result.stderr

    from app.services.briefing_service import BriefingClient
    from app.services.changeset import theme as load_theme

    briefing = BriefingClient({}, str(repo)).load_cached(get_review_hunks(str(repo))[0])
    assert (briefing.summary, briefing.kind, briefing.theme) == ("what", "move", "clamp")
    assert briefing.related[0]["relation"] == "caller_of" and briefing.related[0]["header"] == other["header"]
    assert load_theme(str(repo), "clamp")["why"] == "Zero broke the printer."


def test_writer_rejects_what_the_app_would_silently_mangle(repo: Path):
    target = _load_scanner().scan(str(repo))[0]
    base = [
        "--repo",
        str(repo),
        "--file-path",
        target["file_path"],
        "--header",
        target["header"],
        "--content-hash",
        target["content_hash"],
        "--intent",
        "why",
    ]
    bad_relation = json.dumps([{"file_path": "x.py", "relation": "vaguely_related"}])
    assert _run_skill("write_briefing.py", *base, "--related", bad_relation).returncode != 0
    assert _run_skill("write_briefing.py", *base, "--summary", "s" * 301).returncode != 0
    too_many = json.dumps([{"file_path": f"{i}.py", "relation": "same_edit"} for i in range(6)])
    assert _run_skill("write_briefing.py", *base, "--related", too_many).returncode != 0


def test_writer_warns_about_an_unknown_theme(repo: Path):
    target = _load_scanner().scan(str(repo))[0]
    result = _run_skill(
        "write_briefing.py",
        "--repo",
        str(repo),
        "--file-path",
        target["file_path"],
        "--header",
        target["header"],
        "--content-hash",
        target["content_hash"],
        "--intent",
        "why",
        "--theme",
        "never-written",
    )
    assert result.returncode == 0 and "never-written" in result.stderr


def test_write_theme_replaces_by_id_and_keeps_others(repo: Path):
    for theme_id, why in (("a", "first"), ("b", "other"), ("a", "second")):
        result = _run_skill(
            "write_theme.py",
            "--repo",
            str(repo),
            "--id",
            theme_id,
            "--title",
            "t",
            "--why",
            why,
            "--source",
            "investigated",
        )
        assert result.returncode == 0, result.stderr
    themes = json.loads((repo / ".context" / "changeset.json").read_text(encoding="utf-8"))["themes"]
    assert sorted((t["id"], t["why"]) for t in themes) == [("a", "second"), ("b", "other")]
