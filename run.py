"""Entry point: point it at the git repo you want to review.

    cd ~/your-project && python /path/to/this/app/run.py
    python run.py --repo ~/your-project

With no --repo it reviews the current working directory, which is what
makes the first form work from anywhere.

Serves the review UI at http://127.0.0.1:8765 (port configurable in
app/config.yaml) and opens it in your default browser. If that port is
busy, the next free one is used and the address is printed.

Runs a short preflight first (see app/services/preflight.py) so a broken
setup reports itself in the terminal rather than as a UI that opens and
then does nothing.
"""

from __future__ import annotations

import argparse
import os
import socket
import threading
import webbrowser
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

# Must run before importing app.server (inside main) — server.py reads
# ANTHROPIC_API_KEY (via conversation_service.py) at import time, to build
# its startup-status check. .env is never committed (see .gitignore) and is
# deliberately not something Claude reads when working on this repo — see
# .env.example for the expected shape.
load_dotenv()

# Read by app/web/config.py, which resolves CONFIG once at import: the repo
# path has to be settled before that happens, because the UI-saved settings
# it layers on top are read from inside the repo being reviewed. An env var
# rather than a parameter for the same reason — nothing has been imported
# yet that could take one.
REPO_PATH_ENV = "REVIEW_REPO_PATH"

# How many ports past the configured one to try before giving up. Enough
# for a handful of concurrent reviews, small enough that a genuinely wedged
# machine still fails rather than scanning forever.
_PORT_SEARCH_RANGE = 20


def find_free_port(host: str, preferred: int) -> int:
    """The configured port if it's free, otherwise the next one that is.

    A second copy of the app — reviewing another repo, or left running in
    another terminal — used to make this exit with an unexplained
    "address already in use" traceback. Falling forward keeps the common
    case (one instance on the expected port) unchanged while making the
    second instance just work.
    """
    for candidate in range(preferred, preferred + _PORT_SEARCH_RANGE):
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind((host, candidate))
                return candidate
            except OSError:
                continue
    raise SystemExit(
        f"No free port between {preferred} and {preferred + _PORT_SEARCH_RANGE - 1} on {host}. "
        "Close another instance, or set server.port in app/config.yaml."
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Walk through a repo's uncommitted changes with an AI reviewer.")
    parser.add_argument(
        "--repo",
        help="the git repo to review (default: the current directory, or server.repo_path in app/config.yaml)",
    )
    return parser.parse_args(argv)


def main() -> None:
    args = parse_args()
    if args.repo:
        repo = Path(args.repo).expanduser().resolve()
        if not repo.is_dir():
            raise SystemExit(f"Not starting — --repo {repo} is not a directory.")
        os.environ[REPO_PATH_ENV] = str(repo)

    # Imported here, not at module scope: importing app.server resolves
    # CONFIG, which reads the repo path set just above.
    from app.server import app
    from app.services.preflight import format_report, has_fatal, run_checks
    from app.services.session_store import ensure_artifacts_ignored
    from app.web.config import CONFIG

    host = CONFIG["server"].get("host", "127.0.0.1")
    preferred_port = CONFIG["server"].get("port", 8765)
    # Configurable rather than pinned to loopback, for anyone who deliberately
    # wants remote access — app/web/config.py warns at startup when it isn't
    # loopback, since the app has no authentication of its own.

    checks = run_checks(CONFIG)
    print("AI Pear Review — checking your setup\n")
    print(format_report(checks))
    print()
    if has_fatal(checks):
        raise SystemExit("Not starting — fix the FAIL line(s) above and try again.")

    # After the preflight, because it needs a real repository, and before
    # the first write lands there: this app's own directories should never
    # show up as untracked files in the reviewer's own `git status`.
    added = ensure_artifacts_ignored(CONFIG["server"].get("repo_path", "."))
    if added:
        print(f"Added {', '.join(added)} to this repo's .git/info/exclude (not committed).\n")

    port = find_free_port(host, preferred_port)
    if port != preferred_port:
        print(f"Port {preferred_port} is in use, using {port} instead.\n")
    url = f"http://{host}:{port}"

    # Set REVIEW_NO_BROWSER=1 when something else is driving the UI. An
    # auto-opened tab is a second WebSocket client, and the server cancels
    # the previous client's in-flight LLM work when a new one connects — so
    # under Playwright this tab silently cancels the briefing the test is
    # waiting for.
    if not os.environ.get("REVIEW_NO_BROWSER"):
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    print(f"Opening {url}\n")
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
