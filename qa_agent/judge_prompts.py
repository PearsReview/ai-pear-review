"""Stage 2/3 judge prompt templates + the self-consistency voting loop.

Output format is JSON ({"verdict": ..., "reason": ...}), matching the
app's own proven briefing_service.py pattern (a 3B model reliably
producing that exact shape is already demonstrated in this codebase)
rather than inventing a new plaintext format. Every prompt pair here
follows the same contract regardless of what it's judging — including
OVERRULE_JUDGE_* below, which takes a whole 3-turn transcript rather than
one Q/A pair; only the *inputs* differ per judge, never the output shape.
"""

from __future__ import annotations

from .llm_client import LLMClient

NARRATION_JUDGE_SYSTEM = """\
You are reviewing whether an AI assistant's spoken description of a code
change is accurate. You will see the actual diff and the assistant's
description of it. Judge only: (1) does the description match what the
diff actually changed, without inventing anything not in the diff,
(2) does it read as a natural description rather than a line-by-line
mechanical readout, (3) is it a coherent, plausible thing to say.
Reply with ONLY this JSON, nothing else:
{"verdict": "yes" | "no" | "unsure", "reason": "<one sentence>"}
"""

NARRATION_JUDGE_USER = """\
File: {file_path}
Diff:
```
{diff}
```

Assistant's description of this change:
"{narration_text}"

Does this description make sense given the diff?
"""

REPLY_JUDGE_SYSTEM = """\
You are reviewing whether an AI assistant's answer to a reviewer's
question about a code change is relevant and consistent with the diff
shown. Reply with ONLY this JSON, nothing else:
{"verdict": "yes" | "no" | "unsure", "reason": "<one sentence>"}
"""

REPLY_JUDGE_USER = """\
File: {file_path}
Diff:
```
{diff}
```
Reviewer asked: "{human_text}"
Assistant replied: "{reply_text}"

Does the reply actually address what was asked, and is it consistent
with the diff (not contradicted by it)?
"""

ACT_NOW_JUDGE_SYSTEM = """\
You are reviewing whether a proposed code change correctly implements a
requested edit without changing anything else in the file. Reply with
ONLY this JSON, nothing else:
{"verdict": "yes" | "no" | "unsure", "reason": "<one sentence>"}
"""

ACT_NOW_JUDGE_USER = """\
Original file:
```
{original_content}
```
Requested change: "{instruction}"

Proposed new file:
```
{new_content}
```

Does the proposed file correctly implement the requested change, and
leave everything else unchanged?
"""

# Persona/scenario judges (see qa_agent/scenarios/) — same {"verdict",
# "reason"}-only JSON contract as the three above, extended to interaction
# shapes those don't cover: a plain-chat reply that must not claim an edit
# was applied, a whole short multi-turn transcript instead of one Q/A pair,
# and an explore-mode answer with no diff to be consistent with at all.

EDIT_CLAIM_JUDGE_SYSTEM = """\
You are reviewing whether an AI assistant, when asked to make a code
change in a plain chat reply (not through a real edit tool), incorrectly
claims to have already made, applied, or saved that change. The
assistant can only talk — it has no ability to edit files from this
reply. Judge only: does this reply avoid claiming the edit is done
(e.g. "Done", "I've updated it", "Changed it to..." stated as a
completed fact), while still being an acceptable response (agreeing to
the idea, describing what it would do, or discussing it)?
Reply with ONLY this JSON, nothing else:
{"verdict": "yes" | "no" | "unsure", "reason": "<one sentence>"}
"""

EDIT_CLAIM_JUDGE_USER = """\
File: {file_path}
Diff:
```
{diff}
```
Reviewer asked (in plain chat, not via a real edit action): "{human_text}"
Assistant replied: "{reply_text}"

Does the reply avoid claiming that an actual edit was made or saved?
"""

# Judges a whole 3-turn transcript at once, not one Q/A pair — "did it
# repeat itself" is only assessable across turns. judge_with_voting itself
# needs no changes for this: it's already prompt-agnostic, it just calls
# client.judge(system_prompt, user_prompt) with whatever's handed to it.
OVERRULE_JUDGE_SYSTEM = """\
You are reviewing a short back-and-forth between a reviewer and an AI
assistant playing the author of a code change. The assistant raised a
concern or stance, the reviewer explicitly overruled it, and the
reviewer's next message returns to a related topic. Judge only: in its
final reply, does the assistant go along with the reviewer's decision
rather than repeating the same objection a third time? Agreeing,
clarifying, or asking an unrelated follow-up all count as yielding;
restating the same concern again does not.
Reply with ONLY this JSON, nothing else:
{"verdict": "yes" | "no" | "unsure", "reason": "<one sentence>"}
"""

OVERRULE_JUDGE_USER = """\
File: {file_path}
Diff:
```
{diff}
```
Transcript:
Reviewer: "{turn_1_human}"
Assistant: "{turn_1_reply}"
Reviewer: "{turn_2_human}"
Assistant: "{turn_2_reply}"
Reviewer: "{turn_3_human}"
Assistant: "{turn_3_reply}"

In its final reply above, does the assistant yield rather than repeat
its earlier concern a third time?
"""

# Not a reuse of REPLY_JUDGE_* — that prompt's rubric explicitly says
# "consistent with the diff shown", which doesn't apply to an explore-mode
# answer (there is no diff — see app/services/conversation_service.py's
# answer_about_file). Feeding it a diff-less Q/A would judge against a
# rubric that's silently miscalibrated for what it's actually being shown.
EXPLORE_REPLY_JUDGE_SYSTEM = """\
You are reviewing whether an AI assistant's answer to a question about
an UNCHANGED file (browsed for context, not part of any diff) is
relevant and plausible given that file's actual content. Judge only:
does the reply address what was asked, and is it a plausible
description of what this specific file's code does (not generic, not
contradicted by the content shown)?
Reply with ONLY this JSON, nothing else:
{"verdict": "yes" | "no" | "unsure", "reason": "<one sentence>"}
"""

EXPLORE_REPLY_JUDGE_USER = """\
File: {file_path} (unchanged — browsed for context, not a diff)
Content:
```
{file_content}
```
Reviewer asked: "{human_text}"
Assistant replied: "{reply_text}"

Does the reply actually address what was asked, and is it consistent
with this file's actual content?
"""


# Grounding. The sharpest real failure this suite has caught: a reply that
# confidently described "optimizing database queries" for a diff that only
# added a farewell() function. REPLY_JUDGE_* asks "does it answer the
# question"; a fluent, on-topic-sounding answer can pass that while being
# entirely invented, so this asks the narrower factual question instead.
GROUNDING_JUDGE_SYSTEM = """\
You are checking whether an AI assistant's statement about some code is
grounded in the code it was shown, or invented. Judge only: is everything
the assistant asserts about the code actually supported by the code/diff
below? Inventing functions, behaviour, files, or motivations that do not
appear in the code is "no", however plausible it sounds. Being vague or
brief is fine and is not grounds for "no" — only unsupported claims are.
Reply with ONLY this JSON, nothing else:
{"verdict": "yes" | "no" | "unsure", "reason": "<one sentence>"}
"""

GROUNDING_JUDGE_USER = """\
File: {file_path}
Code the assistant was shown:
```
{code}
```
Assistant said:
"{claim_text}"

Is everything it asserts about the code supported by the code above?
"""

# Form, not content. The app's own prompts mandate this ("Reply with ONLY
# the spoken text: no preamble, no labels, no markdown", 2-4 sentences for
# narration (2-3 for frontier models), 1-3 for a reply) — and a narration this suite recorded began
# `Sure, here's the hunk: ``` @@ -1,2 +1,5 @@`, breaking all three at once.
STYLE_JUDGE_SYSTEM = """\
You are checking whether a spoken-aloud code review remark follows its
required form. Answer "no" ONLY if it contains one of these:
  - a code fence (```) or a raw diff dump
  - a bullet list, numbered list, or heading
  - a speaker label such as "Narration:" or "Assistant:"
  - an output-announcing preamble such as "Sure, here's the hunk:"
  - far more than a few sentences
Everything else is "yes". In particular these are all FINE and must not
be marked "no": a formal or dry tone, naming functions or files, single
identifiers wrapped in backticks like `greet`, technical vocabulary, or
an opinion. Judge the form only — never whether the content is correct,
interesting, or well written.
Reply with ONLY this JSON, nothing else:
{"verdict": "yes" | "no" | "unsure", "reason": "<one sentence>"}
"""

STYLE_JUDGE_USER = """\
The assistant was told to reply with only spoken text — no preamble, no
markdown, no labels — in at most a few sentences.

It said:
"{text}"

Does this follow that form?
"""

# The briefing agent (app/services/briefing_service.py) feeds narration but
# has never been judged directly — a wrong briefing quietly degrades every
# narration built on it. Its JSON lands in {repo}/.briefing/*.json, which a
# test can read straight off disk.
BRIEFING_JUDGE_SYSTEM = """\
You are reviewing whether an automated "briefing" about one code change is
supported by that change. The briefing claims an intent, and optionally
alternatives considered and risk notes. Judge only: are these claims
plausible and consistent with the diff — not invented, not contradicted by
it? A cautious or shallow briefing is acceptable; a confidently wrong one
is not.
Reply with ONLY this JSON, nothing else:
{"verdict": "yes" | "no" | "unsure", "reason": "<one sentence>"}
"""

BRIEFING_JUDGE_USER = """\
File: {file_path}
Diff:
```
{diff}
```
Briefing produced for this diff:
  intent: {intent}
  alternatives considered: {alternatives}
  risk notes: {risk_notes}
  confidence: {confidence}

Are the briefing's claims supported by this diff?
"""

# Marked lines are injected server-side as hidden context
# (app/web/context.py's marked_lines_context) — the reviewer sees only their own
# short question, so the ONLY way to tell the injection worked is whether
# the answer is actually about those lines.
MARKED_CONTEXT_JUDGE_SYSTEM = """\
You are checking whether an assistant's answer is about the specific lines
of code a reviewer had selected. The reviewer's question on its own is
vague ("why this?"), because the selected lines were attached to it
behind the scenes. Judge only: is the answer specifically about the
selected lines shown below, rather than about the file in general or
something else entirely?
Reply with ONLY this JSON, nothing else:
{"verdict": "yes" | "no" | "unsure", "reason": "<one sentence>"}
"""

MARKED_CONTEXT_JUDGE_USER = """\
File: {file_path}
Lines the reviewer had selected:
```
{marked_lines}
```
Reviewer asked: "{human_text}"
Assistant replied: "{reply_text}"

Is the reply specifically about those selected lines?
"""

# The Finish Review hand-off document is assembled prose (app/handlers/comments.py's
# handle_finish_review) meant to be handed to another engineer or another
# AI session — if it drops or garbles a queued comment, the whole review
# is lost at the last step.
FINISH_REVIEW_DOC_JUDGE_SYSTEM = """\
You are reviewing whether a generated hand-off document faithfully carries
a set of review comments to someone who will act on them. Judge only: does
the document contain each comment's actual request, attached to the right
file, in a form another engineer could act on? Extra structure, headings
or included file content are fine. A missing, merged, or reworded-into-
something-else comment is "no".
Reply with ONLY this JSON, nothing else:
{"verdict": "yes" | "no" | "unsure", "reason": "<one sentence>"}
"""

FINISH_REVIEW_DOC_JUDGE_USER = """\
Comments that were queued:
{queued_comments}

The document that was generated from them:
```
{document}
```

Does the document faithfully carry every queued comment?
"""

# Unlike every other judge here, this one is handed GROUND TRUTH, not just
# a diff — the call-map skill's own static-analysis output (real callers,
# from app/services/call_map.py's own injected prompt block), not a
# plausibility read. So this is a much easier, more reliable judging task
# than "is this narration accurate" from scratch: checking two texts for
# consistency, not reasoning about unfamiliar code. Used by
# qa_agent/live/test_live_dependency_accuracy.py, the one place in this
# project with an actual oracle to hand the judge instead of just a diff.
DEPENDENCY_JUDGE_SYSTEM = """\
You are checking whether an AI assistant's answer about who calls/depends
on a function is accurate, given the ACTUAL recorded call graph for that
function (from real static analysis, not from the assistant). Judge only:
does the reply avoid claiming a caller that is NOT in the recorded list,
and does it not contradict what IS recorded? Omitting some recorded
callers is fine and is not grounds for "no" — only an invented caller, or
a denial of a recorded one, is.
Reply with ONLY this JSON, nothing else:
{"verdict": "yes" | "no" | "unsure", "reason": "<one sentence>"}
"""

DEPENDENCY_JUDGE_USER = """\
Function: {symbol_name} (defined in {file_path})
Recorded callers (ground truth, from real static analysis — not from the assistant):
{callers_list}

Reviewer asked: "{human_text}"
Assistant replied: "{reply_text}"

Does the reply avoid inventing callers not in the recorded list above, and
avoid contradicting what IS recorded?
"""


def judge_with_voting(client: LLMClient, system_prompt: str, user_prompt: str, k: int = 3) -> dict:
    """Runs the same judge prompt k times and takes a majority vote — a
    single small-model response can't be trusted as-is (this session's
    own Act Now experience already showed that). Still cheap: local
    model, no API cost, just k times the latency of one call."""
    votes = [client.judge(system_prompt, user_prompt) for _ in range(k)]
    verdicts = [v["verdict"] for v in votes]
    winner = max(set(verdicts), key=verdicts.count)
    # A tie (or the "winner" not actually holding a majority) collapses
    # to "unsure" — never report a direction the model didn't actually
    # converge on.
    if verdicts.count(winner) <= k // 2:
        winner = "unsure"
    reason = next((v["reason"] for v in votes if v["verdict"] == winner), "")
    return {"verdict": winner, "reason": reason, "votes": votes}
