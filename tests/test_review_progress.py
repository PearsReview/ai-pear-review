"""review_progress's per-file hunk list (app/web/progress.py)."""

from __future__ import annotations

import asyncio

from app.services.diff_service import Hunk
from app.web.progress import send_review_progress
from app.web.session import Session


class FakeSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)


def _hunk(index: int, file_path: str, new_start: int) -> Hunk:
    header = f"@@ -{new_start},1 +{new_start},1 @@"
    return Hunk(index=index, file_path=file_path, header=header, lines=[header, "+x"])


def test_each_file_lists_its_hunks_with_their_reviewed_state(tmp_path):
    session = Session([_hunk(0, "a.py", 3), _hunk(1, "a.py", 40), _hunk(2, "b.py", 1)], None, str(tmp_path))
    session.reviewed = {1}
    ws = FakeSocket()
    asyncio.run(send_review_progress(ws, session))

    files = ws.sent[0]["payload"]["files"]
    assert [f["file_path"] for f in files] == ["a.py", "b.py"]
    assert files[0]["hunks"] == [
        {"index": 0, "header": "@@ -3,1 +3,1 @@", "reviewed": False},
        {"index": 1, "header": "@@ -40,1 +40,1 @@", "reviewed": True},
    ]
    assert files[0]["reviewed_count"] == 1 and files[1]["hunks"][0]["index"] == 2
