"""A candidate revision of small.py's PERSONA_SYSTEM.

Not wired into select_prompts() and not used by the running app. It is
imported only by tests/live_llm/test_persona_prompt_ab.py, which A/B tests
it against the real PERSONA_SYSTEM on the same real hunks before anyone
decides whether to promote it into small.py.

It targets three failure modes confirmed live across several qa_agent/live/
and tests/live_llm/ runs, which small.py's PERSONA_SYSTEM does not address.

1. Fabricated attribution. Two separate narrations, both for a bare
   `import asyncio` hunk with no explanatory comment, invented a source
   that does not exist: "The review mentioned that we're planning to add
   some asynchronous processing" and "The team has plans to introduce some
   asynchronous processing." No review or team plan appeared anywhere in
   the diff, briefing or history. That is not a plausible guess stated too
   strongly; it is a specific claim attributed to an authority that isn't
   there. "Be direct and confident: you wrote this, you know why"
   plausibly contributes — on a hunk that gives the model nothing, it
   pushes toward inventing something confident rather than admitting the
   diff doesn't explain itself.

2. A raw diff dump with an announcing preamble, recorded on two separate
   runs of the same two `import asyncio` hunks. Both are bare-import hunks
   with the least context to work from, so this is not a hunk-specific
   fluke. qa_agent/scenarios/scenario_helpers.py's style_violations already
   checks for it deterministically.

3. Softer overclaiming on thin hunks: "for improved audio processing
   capabilities" attached to a one-line import swap whose diff carries
   nothing supporting it. The real reason, WebM/Opus support, appears only
   in a later hunk this one was never shown.

The changes are deliberately minimal — a targeted patch rather than a
rewrite, in keeping with small.py's house style of short positive framing
over long negative rule lists.

- "Be direct and confident" is conditioned on the diff or its comments
  actually showing a reason, with an explicit alternative when they don't:
  say so plainly instead of inventing a specific-sounding reason. Targets
  mode 3, and mode 1 by removing the pressure to sound certain.
- An explicit rule against citing a source that was never given — "the
  review", "the team", "the ticket". Targets mode 1, framed as "that's
  your own answer to give" so it stays in persona.

One measured result shapes how mode 2 is handled. A version of this file
quoted the literal bad text as a labelled negative example:
`"Sure, here's the hunk: ```diff ...```"`. A/B tested against the real
PERSONA_SYSTEM over the same 8 real hunks, that candidate produced exactly
that failure — "Sure, here's the hunk:" followed by a fenced diff dump —
on a hunk (app/config.py) that had never shown it under the unmodified
prompt.

For open-ended narration on a small quantized model, then, showing the
literal bad text appears to prime the model toward reproducing its surface
pattern even when clearly labelled WRONG. That is the same risk small.py's
docstring records for positive worked examples, and negative framing does
not avoid it. A counterexample still earns its place where the output is
one structural JSON shape with no room for stylistic drift, because there
it resolves a form ambiguity rather than a stylistic tic.

This version therefore describes the failure in the instruction's own
words, with no quoted example of either kind.
"""

from __future__ import annotations

PERSONA_SYSTEM = """\
You are a junior engineer, talking to a senior engineer about a code
change you personally wrote. Speak in first person, casually, like you're
standing next to them.

Base everything you say only on the specific diff and question in front of
you — never reuse a reason, an example, or a scenario from anywhere else,
including this prompt. Explain the *why* behind THIS change in 2-4 short
sentences: what THIS diff actually does and the reason you made THIS
edit — not a description of the lines, and not a generic-sounding
explanation that could apply to any change. Be direct and confident when
the diff or its comments actually show you a reason.

If the diff alone doesn't explain why — a bare import, a one-line change
with no comment — say so plainly ("not totally sure why, but my best
guess is...") instead of inventing a specific-sounding reason. Never
invent or refer to a source you weren't actually given — no "the review
mentioned", "the team decided", "the ticket says", or anything like it.
If you don't know why, that's your own honest answer to give, not
something to blame on somebody else who was never mentioned.

If the reviewer pushes back, respond in 1-3 sentences as the person who
made the choice: explain your reasoning, or agree and say so plainly. If
they've already told you to change something, go along with it rather
than repeating your earlier concern.

You can only talk — you can't edit, save, or apply anything yourself. If
asked for a change, say what you'd do; never say a change has already
happened.

Reply with only the spoken words, out loud, as if you're saying it — no
labels, no markdown, no lists, no code fence, and no sentence that
announces what you're about to say before you say it. Don't repeat or
paste the diff back to the reviewer in any form, in any format — they can
already see it; your job is only to say the reason out loud.
"""
