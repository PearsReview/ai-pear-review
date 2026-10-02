"""Shared helpers for qa_agent's realistic-scenario tests (see the sibling
test_messy_navigation.py / test_persona_behavior.py /
test_explore_mode_conversation.py). Every scenario here builds on the same
page_with_ws + WsFrames + judge_with_voting machinery Stage 2's
test_semantic_quality.py already established one directory up — this module
exists so that machinery's repeated boilerplate (fill/send/wait-for-reply, a
shared TTS-frame counter, the "never claims to have made an edit" check)
doesn't get re-typed per scenario file.
"""

from __future__ import annotations

import re
import time


def wait_for_new_frame(frames, page, msg_type: str, since_index: int, timeout_ms: int = 30_000) -> dict:
    """Like WsFrames.wait_for (ws_capture.py), but scoped to frames arriving
    strictly after since_index. Needed anywhere a test waits for the same
    msg_type more than once in one test — a second "presenting" after
    switching hunks, a second/third "reviewer_turn" in a multi-turn
    exchange — because WsFrames.wait_for returns the newest EXISTING match
    the instant any match exists at all; called again later in the same
    test it would immediately return a frame captured earlier instead of
    actually waiting for a new one, since that earlier frame already
    satisfies "matches is non-empty" on its very first poll. Mirrors
    test_semantic_quality.py's own _wait_for_act_now_result, generalized
    from an either/or pair of message types to any single one. Callers
    capture since_index = len(frames.frames) *before* the action that
    should produce the new frame."""
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        for frame in frames.frames[since_index:]:
            if frame.get("type") == msg_type:
                return frame["payload"]
        page.wait_for_timeout(100)
    raise TimeoutError(f"no {msg_type!r} frame received within {timeout_ms}ms (since frame index {since_index})")


def ask_and_capture_reply(page, frames, question: str, timeout_ms: int = 60_000) -> dict:
    """Fills #text-input, sends it, and waits for the reviewer_turn reply
    that arrives *after* this send — the same fill/send/wait-for sequence
    every conversational scenario needs at least once, several of them 2-3
    times per test for a multi-turn exchange, which is exactly why this
    uses wait_for_new_frame (above) rather than WsFrames.wait_for directly:
    a second/third call in the same test must not immediately re-return an
    earlier turn's reply. Returns the reviewer_turn WS frame's payload (has
    "text" among other fields — see app/handlers/narration.py's handle_reply).

    The default was 30s and is now 60s, matching the narration waits
    elsewhere in this suite. Every caller was on the default, so this was a
    suite-wide flake rather than one test's problem: Ollama serializes per
    model, so in a combined run a reply queues behind other tests' calls
    and 30s runs out. Observed exactly that —
    test_explore_mode_conversation timed out at 30s inside a full scenarios
    run and passed alone in ~39s end to end. Explore replies are the worst
    case (they send the whole file, see _file_prompt), but nothing here
    makes a *smaller* request than a hunk reply, so the budget is raised
    once here rather than at nine call sites."""
    start_index = len(frames.frames)
    page.fill("#text-input", question)
    page.click("#send-btn")
    return wait_for_new_frame(frames, page, "reviewer_turn", start_index, timeout_ms=timeout_ms)


def tts_status_frame_count(frames) -> int:
    """service_status frames carrying a "tts" key are sent at most once per
    genuine try_speak() attempt (success or failure — see app/web/speech.py's
    try_speak), and never for a skipped one. Counting them is a signal that
    tolerates the TTS backend being up or down in this environment: either
    way, a real attempt sends exactly one such frame, a skipped one sends
    none. Relocated here from test_conversation_dedup.py (which imports it
    back — see that file) since test_messy_navigation.py's multi-hunk
    revisit scenario needs the identical count and duplicating the function
    would let the two drift apart."""
    return sum(1 for f in frames.frames if f.get("type") == "service_status" and "tts" in f.get("payload", {}))


# Phrasing that would indicate the persona claimed to have actually made,
# applied, or saved an edit — it can only talk (see app/prompts/frontier.py
# and small.py's explicit "never claim you've made an edit" rule). Kept
# narrow and high-precision on purpose: a false positive here fails a test
# for nothing, and judge_prompts.py's EDIT_CLAIM_JUDGE_* is the nuanced
# backstop for anything phrased indirectly enough to slip past this — this
# only needs to catch the sharpest, most literal violations. Mirrors the
# "deterministic check layered on top of an LLM judge" precedent
# test_semantic_quality.py's _assert_only_target_region_changed already
# established for Act Now.
_EDIT_CLAIM_DENYLIST_RE = re.compile(
    r"""
    (?:^|[.!?]\s+)done[.!]?\s*$                                    # a reply that IS (or ends in) a bare "Done."
    |i['’]?ve\s+(?:made|applied|updated|added|changed|fixed|saved)\s+(?:it|that|this|the\ change)
    |(?:the\ )?change(?:d)?\s+(?:is|has\ been)\s+(?:now\ )?(?:made|applied|saved)
    |it['’]?s\s+(?:now\ )?(?:updated|fixed|done|saved)
    """,
    re.IGNORECASE | re.VERBOSE,
)


# Form violations sharp enough to be worth catching exactly rather than
# asking a model about: a markdown code fence, a raw diff hunk header, or a
# "Sure, here's..."-style preamble. All three are explicitly forbidden by
# the app's own persona prompt ("Reply with ONLY the spoken text: no
# preamble, no labels, no markdown"), and all three have actually shown up
# — this suite recorded a narration beginning "Sure, here's the hunk: ```
# @@ -1,2 +1,5 @@". STYLE_JUDGE_* (judge_prompts.py) remains the nuanced
# half; this is the part that needs no judgment at all.
_STYLE_VIOLATION_PATTERNS = (
    ("markdown code fence", re.compile(r"```")),
    ("raw diff hunk header", re.compile(r"^\s*@@ -\d", re.MULTILINE)),
    # An *announcing* opener, not merely a friendly one: "Sure, here's the
    # hunk:" breaks the form, while "Sure, that makes sense to me." is
    # ordinary speech and must not trip this. The colon is what separates
    # them — it's the "output follows" signal — so a bare comma after
    # "Sure" deliberately doesn't match (an earlier, looser version of this
    # pattern flagged exactly that sentence, which is why the rule is
    # anchored on the colon now).
    (
        "canned preamble",
        re.compile(r"^\s*(?:sure|certainly|of course|okay|ok)\b[^.!?\n]*:", re.IGNORECASE),
    ),
    # The colon rule above misses the version that announces the output as a
    # complete sentence: the generated-repo suite recorded a narration
    # opening `Sure, I'll present this hunk.` followed by a blank line and
    # the actual narration. That is a preamble by any reading — the persona
    # prompt says reply with ONLY the spoken text — but it has no colon, so
    # nothing caught it. Anchored on a first-person announcement of the act
    # rather than on punctuation, which keeps "Sure, that makes sense to
    # me." (ordinary speech, and the false positive the colon rule exists to
    # avoid) out of it.
    (
        "announces the act instead of doing it",
        re.compile(
            r"^\s*(?:sure|certainly|of course|okay|ok)\b[,.]?\s*"
            # "let's" added after a live run recorded "Sure, let's go
            # through this hunk together." slipping past this pattern —
            # found by app/utils/markdown_speech.py's own cross-check test
            # (tests/test_persona_sanitizing.py), which runs both this
            # module's and that one's patterns over the same real corpus
            # and requires them to agree. Keep the two in sync.
            r"(?:i(?:'|’)?ll|i\s+will|let(?:'|’)?s|let\s+me|i\s+can)\b",
            re.IGNORECASE,
        ),
    ),
    ("output-announcing opener", re.compile(r"^\s*here(?:'|’)?(?:s| is)\b", re.IGNORECASE)),
    ("speaker label", re.compile(r"^\s*(narration|assistant|reply|answer)\s*:", re.IGNORECASE)),
)


def style_violations(text: str) -> list[str]:
    """Names of the deterministic form rules `text` breaks, if any — see
    _STYLE_VIOLATION_PATTERNS. Returns a list rather than asserting so a
    caller can decide: a scenario judging *narration* form wants to fail on
    these, while one that merely records style alongside another judgment
    wants them as context."""
    return [name for name, pattern in _STYLE_VIOLATION_PATTERNS if pattern.search(text)]


def assert_no_style_violations(text: str) -> None:
    """Hard-fails on the unambiguous form breaches above. Paired with
    STYLE_JUDGE_* the same way assert_no_edit_claim_language is paired with
    EDIT_CLAIM_JUDGE_*: the regex catches what needs no judgment, the judge
    catches everything subtler."""
    violations = style_violations(text)
    assert not violations, (
        f"spoken reply breaks its own required form ({', '.join(violations)}) — the persona prompt "
        f"asks for spoken text only, no markdown/preamble/labels: {text!r}"
    )


def assert_no_edit_claim_language(reply_text: str) -> None:
    """Deterministic pre-filter for the "never claims to have made an edit"
    persona rule — cheap, exact, and guards a failure mode the app's own
    prompts explicitly call out. Not a replacement for EDIT_CLAIM_JUDGE_*
    (judge_prompts.py) — an implied or indirectly-phrased claim this regex
    can't catch is still that judge's job; this only catches the sharpest,
    most literal version, the same division of labor
    _assert_only_target_region_changed already established for Act Now."""
    match = _EDIT_CLAIM_DENYLIST_RE.search(reply_text)
    assert match is None, (
        f"reply appears to claim an edit was made (matched {match.group(0)!r}), "
        f"but the persona can only talk — it never applies edits from a plain "
        f"chat reply: {reply_text!r}"
    )
