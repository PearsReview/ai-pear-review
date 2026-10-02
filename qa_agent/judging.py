"""One place where a judgment is run AND recorded, with full provenance.

Stage 2 (test_semantic_quality.py) and Stage 3 (scenarios/) both used to
do the same three steps inline — vote with judge_with_voting, then hand the
verdict plus a hand-built context dict to record_finding — which meant the
judge's own prompt was never recorded anywhere, and adding the app's
captured prompt would have meant editing every call site the same way
again. Composing it here instead keeps judge_prompts.py to prompts+voting
and findings_log.py to writing lines, while making "a finding always
carries what both models were given" true by construction rather than by
each caller remembering.
"""

from __future__ import annotations

from pathlib import Path

from .findings_log import record_finding
from .judge_prompts import judge_with_voting
from .llm_capture import find_capture_for_response
from .llm_client import LLMClient


def judge_and_record(
    *,
    category: str,
    client: LLMClient,
    system_prompt: str,
    user_prompt: str,
    context: dict,
    response_text: str | None = None,
    capture_dir: Path | None = None,
    k: int = 3,
) -> dict:
    """Votes k times on (system_prompt, user_prompt), records the result
    under `category`, and returns the verdict.

    response_text + capture_dir are how the app's own side gets attached:
    given the text the app produced (a narration/reviewer_turn payload) and
    the directory it writes captures to (conftest.py's llm_capture_dir),
    the matching call — verbatim system prompt, message list, token counts
    — is looked up and stored on the finding. Both are optional and
    independently so: capture may be off, and some categories (act_now,
    overrule) judge something other than a single response, so they simply
    omit it rather than pretending a lookup key exists.

    The judge side is recorded unconditionally, since it's always known
    here: which model ruled, and the exact two prompts it ruled on."""
    verdict = judge_with_voting(client, system_prompt, user_prompt, k=k)
    llm_call = None
    if capture_dir is not None and response_text:
        llm_call = find_capture_for_response(capture_dir, response_text)
    record_finding(
        category,
        verdict,
        context,
        llm_call=llm_call,
        judge={"model": client.model, "system": system_prompt, "user": user_prompt},
    )
    return verdict
