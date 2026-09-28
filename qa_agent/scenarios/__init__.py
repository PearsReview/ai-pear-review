"""Realistic, hand-scripted navigation + conversation scenarios (see
qa_agent/README.md's "Stage 3" section) — a deliberate divergence from the
original "an LLM agent decides its own click/type sequence" Stage 3 design,
which stays deferred. Every test here is a fixed Playwright test function,
same idiom as qa_agent/'s Stage 1 files, that combines multiple actions
(navigation + a live conversation) and/or judges a whole short transcript
instead of one Q/A pair — see scenario_helpers.py for the shared machinery.
"""
