"""Refresh Diff prunes the briefing cache instead of deleting it.

Deleting .briefing/ wholesale used to throw away every prep-review briefing
on each refresh — including on every Act Now confirm — for hunks that hadn't
changed at all. Pruning keeps exactly what a current hunk would still load.
"""

from __future__ import annotations

import json

from app.services.briefing_service import (
    BriefingClient,
    _hunk_content_hash,
    _pregenerated_briefing_path,
    prune_briefing_cache,
)
from app.services.diff_service import Hunk


def _hunk(file_path: str, header: str, body: str) -> Hunk:
    return Hunk(index=0, file_path=file_path, header=header, lines=[header, body])


def _write(tmp_path, hunk: Hunk, content_hash: str) -> None:
    path = _pregenerated_briefing_path(str(tmp_path), hunk)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"content_hash": content_hash, "intent": "why", "confidence": "high"}), encoding="utf-8")


def test_keeps_matching_and_removes_orphaned_stale_and_unreadable(tmp_path):
    kept = _hunk("a.py", "@@ -1,1 +1,1 @@", "+kept")
    stale = _hunk("b.py", "@@ -1,1 +1,1 @@", "+new body")
    orphan = _hunk("c.py", "@@ -9,1 +9,1 @@", "+gone")
    _write(tmp_path, kept, _hunk_content_hash(kept.diff_context))
    _write(tmp_path, stale, _hunk_content_hash("+old body"))
    _write(tmp_path, orphan, _hunk_content_hash(orphan.diff_context))
    junk = tmp_path / ".briefing" / "junk.json"
    junk.write_text("{broken", encoding="utf-8")

    prune_briefing_cache(str(tmp_path), [kept, stale])

    assert _pregenerated_briefing_path(str(tmp_path), kept).exists()
    assert not _pregenerated_briefing_path(str(tmp_path), stale).exists()
    assert not _pregenerated_briefing_path(str(tmp_path), orphan).exists()
    assert not junk.exists()
    assert BriefingClient({}, str(tmp_path)).load_cached(kept) is not None


def test_no_cache_dir_is_fine(tmp_path):
    prune_briefing_cache(str(tmp_path), [])
