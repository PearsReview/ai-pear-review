"""Starts the review backend for the extension.

    python launch.py --repo <git repo>

run.py's startup, minus the browser, plus one machine-readable line the
extension waits for: `PEAR_REVIEW_PORT=<port>` once the port is chosen.
The extension then polls the server until it answers, so this never has
to report readiness itself.

Reuses run.py's own pieces (find_free_port, the preflight,
ensure_artifacts_ignored) rather than copying them, so the two entry
points can't drift.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# PEAR_REVIEW_BACKEND_DIR is for the integration tests, which run a copy of the
# backend with a patched config.yaml (fake model, fake agent), as qa_agent does,
# so the repo's own backend is never edited. Otherwise: the backend/ that
# `npm run sync-backend` copies in (what the .vsix holds), or, running from a
# checkout, the repo root this extension lives in.
_EXTENSION_ROOT = Path(__file__).resolve().parent.parent
_PACKAGED = _EXTENSION_ROOT / "backend"
BACKEND = Path(
    os.environ.get("PEAR_REVIEW_BACKEND_DIR") or (_PACKAGED if _PACKAGED.is_dir() else _EXTENSION_ROOT.parent)
)
sys.path.insert(0, str(BACKEND))


def main() -> None:
    import run

    args = run.parse_args()
    if not args.repo:
        raise SystemExit("launch.py needs --repo.")
    repo = Path(args.repo).expanduser().resolve()
    if not repo.is_dir():
        raise SystemExit(f"Not starting — --repo {repo} is not a directory.")
    os.environ[run.REPO_PATH_ENV] = str(repo)

    # Importing run.py loaded the backend's own .env, as the web app does. The reviewed
    # repo's .env is never loaded: it is input from that repo, and its variables would
    # reach the coding agent's process (NODE_OPTIONS, ANTHROPIC_BASE_URL, ...).

    import uvicorn

    from app.server import app
    from app.services.preflight import format_report, has_fatal, run_checks
    from app.services.session_store import ensure_artifacts_ignored
    from app.web.config import CONFIG

    checks = run_checks(CONFIG)
    print(format_report(checks), flush=True)
    if has_fatal(checks):
        raise SystemExit("Not starting — fix the FAIL line(s) above and try again.")
    ensure_artifacts_ignored(CONFIG["server"].get("repo_path", "."))

    host = CONFIG["server"].get("host", "127.0.0.1")
    port = run.find_free_port(host, CONFIG["server"].get("port", 8765))
    print(f"PEAR_REVIEW_PORT={port}", flush=True)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
