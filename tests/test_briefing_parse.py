"""A generated briefing survives the reply shapes a provider without
schema-constrained output produces — Anthropic can wrap the JSON in a
markdown fence, which a bare json.loads rejected, silently leaving the hunk
with no briefing at all."""

from __future__ import annotations

import pytest

from app.prompts import FRONTIER
from app.services.briefing_service import BriefingClient
from app.services.conversation_service import ConversationError
from app.services.diff_service import Hunk

HUNK = Hunk(index=0, file_path="billing.py", header="@@ -1,1 +1,1 @@", lines=["@@ -1,1 +1,1 @@", "-a", "+b"])


class FakeConversation:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.prompts = FRONTIER

    def generate_once(self, prompt, *, system_prompt, max_tokens, response_schema):
        return self.reply


FENCED = 'Here is the briefing:\n```json\n{"intent": "Charge tax once", "confidence": "high"}\n```\nDone.'


def test_a_fenced_reply_still_becomes_a_briefing(tmp_path):
    briefing = BriefingClient({}, str(tmp_path)).analyze_hunk(HUNK, FakeConversation(FENCED))
    assert briefing.intent == "Charge tax once"
    assert briefing.confidence == "high"
    assert briefing.source == "generated"


def test_a_reply_with_no_json_is_still_an_error(tmp_path):
    with pytest.raises(ConversationError):
        BriefingClient({}, str(tmp_path)).analyze_hunk(HUNK, FakeConversation("Sorry, I can't do that."))
