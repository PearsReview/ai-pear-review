"""context_scale (app/web/context.py): how much background a session can
afford, as a multiplier on caps measured against the local 8k window."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.diff_service import Hunk
from app.web.context import context_scale
from app.web.session import Session

HUNK = Hunk(index=0, file_path="billing.py", header="@@ -1,1 +1,1 @@", lines=["+x"])


@pytest.mark.parametrize(
    "conversation,expected",
    [
        (None, 1),  # degraded mode — nothing is sent anyway
        (SimpleNamespace(num_ctx=8192), 1),  # a local window to budget against
        (SimpleNamespace(num_ctx=None), 6),  # a provider managing its own window
    ],
)
def test_context_scale_follows_the_window_the_session_has(tmp_path, conversation, expected):
    """The caps were all measured against qwen-7b at num_ctx 8192. Applied
    unchanged to a 1M-token model they throw away context for nothing — but
    a session with no model at all must not read as "huge window", which is
    why this keys on num_ctx rather than prompt_budget (both are None in the
    degraded case)."""
    session = Session([HUNK], conversation, str(tmp_path))
    assert context_scale(session) == expected
