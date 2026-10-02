"""qa_agent — the Playwright-driven UI regression/QA suite for this app.

Deliberately isolated from app/ and static/: nothing here imports app.*,
and the app never imports qa_agent. It only ever talks to the running app
the way an external actor would — driving the real browser, and (in later
stages) its own independent LLM client — never by calling server-side
Python functions in-process.
"""
