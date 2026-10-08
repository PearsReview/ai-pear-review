"""tests/ is the plain, fast pytest suite that imports app.* directly (see
the top-level README's Testing section) — no real model calls, no
external repo dependency, safe to run in any environment.

tests/live_llm/ is a different kind of thing living under this same tree
because it also imports app.* directly (see its own conftest.py's module
docstring for why that puts it here rather than under qa_agent/): it makes
real Ollama calls against a real external --target-repo, takes real
minutes, and needs its own explicit invocation — `pytest tests/live_llm/`
— same reasoning qa_agent/conftest.py's own collect_ignore excludes
qa_agent/generated/ and qa_agent/live/ from `pytest qa_agent/` for.
Without this, a plain `pytest tests/` would silently start making live
Ollama calls against an external repo, which is exactly the
surprise this file exists to prevent.

REVIEW_USER_CONFIG is pointed at a file that doesn't exist before anything
imports app.web.config (which resolves CONFIG at import), so a developer's own
~/.config/pear-review/config.yaml can't change what this suite sees.
"""

import os
from pathlib import Path

os.environ["REVIEW_USER_CONFIG"] = str(Path(__file__).parent / "no-user-config.yaml")

collect_ignore = ["live_llm"]
