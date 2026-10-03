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
# so the submodule itself is never edited.
BACKEND = Path(os.environ.get("PEAR_REVIEW_BACKEND_DIR") or Path(__file__).resolve().parent.parent / "backend")
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

    # load_dotenv ran when run.py was imported, from the extension host's
    # cwd; the reviewed repo's own .env is the one that should apply.
    from dotenv import load_dotenv

    load_dotenv(repo / ".env")

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
