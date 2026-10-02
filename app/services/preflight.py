"""Checks the things a first run actually fails on, and says how to fix them.

Every failure mode here already had a code path — the app degrades rather
than crashes when git is missing, when the repo has no changes, or when the
model server is unreachable. What it didn't have was a way for a
first-time user to find out *which* of those happened. Ollama not running
looks identical to Ollama running the wrong model: the UI opens, the diff
may or may not be there, and nothing narrates.

So this is about discoverability, not safety. It runs once at startup, in
the terminal the user just typed a command into, which is where someone
whose install went wrong is already looking.

Deliberately non-blocking except for the two conditions where continuing is
pointless (no git, not a repo). A model server that is down right now may
be up in a minute, and a repo with no changes may have some as soon as the
reviewer saves a file — refusing to start in either case would be wrong,
and both are already handled gracefully once the UI is open.

No check here is allowed to raise: a preflight that crashes the app it was
meant to reassure you about is worse than no preflight.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..providers.base import DEFAULT_PROVIDER

# Short. These run before the UI opens, and every second here is a second
# of a blank terminal — a slow or wedged model server should be reported as
# unreachable quickly rather than held onto.
_TIMEOUT_SECONDS = 5


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    fix: str | None = None
    fatal: bool = False


def run_checks(config: dict) -> list[Check]:
    """Every check, in the order a user would hit them. Stops early only
    when a later check couldn't mean anything — there is nothing useful to
    say about a repo's diff when git isn't installed."""
    checks: list[Check] = []

    git = _check_git_available()
    checks.append(git)
    if not git.ok:
        return checks

    repo_path = config.get("server", {}).get("repo_path", ".")
    repo = _check_is_git_repo(repo_path)
    checks.append(repo)
    if not repo.ok:
        return checks

    checks.append(_check_has_changes(repo_path))
    checks.extend(_check_model_provider(config.get("conversation", {})))
    return checks


def _check_git_available() -> Check:
    path = shutil.which("git")
    if path:
        return Check("git", True, f"found at {path}")
    return Check(
        "git",
        False,
        "not found on PATH",
        fix="Install git and reopen your terminal so PATH picks it up.",
        fatal=True,
    )


def _check_is_git_repo(repo_path: str) -> Check:
    resolved = Path(repo_path).resolve()
    try:
        result = subprocess.run(
            ["git", "-C", str(resolved), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
            timeout=_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Check(
            "repository",
            False,
            f"could not be read ({exc})",
            fix=f"Check that {resolved} exists and is readable.",
            fatal=True,
        )
    if result.returncode != 0:
        return Check(
            "repository",
            False,
            f"{resolved} is not a git repository",
            fix=(
                "Run this from inside the repo you want to review, or set "
                "server.repo_path in app/config.yaml to point at one."
            ),
            fatal=True,
        )
    return Check("repository", True, result.stdout.strip())


def _check_has_changes(repo_path: str) -> Check:
    """Whether there is anything to review right now.

    Counts untracked files too, because the app presents each one as a
    whole-file hunk — a repo whose only changes are new files still has
    plenty to review, and reporting "no changes" there would be wrong.
    """
    try:
        result = subprocess.run(
            ["git", "-C", repo_path, "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=False,
            timeout=_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return Check("changes", True, "could not be counted — carrying on")
    changed = [line for line in result.stdout.splitlines() if line.strip()]
    if changed:
        return Check("changes", True, f"{len(changed)} file(s) with uncommitted changes")
    return Check(
        "changes",
        False,
        "the working tree is clean",
        fix=(
            "There is nothing to review yet. Edit something and click Refresh Diff, "
            "or point server.repo_path at a repo with uncommitted work."
        ),
    )


def _check_model_provider(conversation: dict) -> list[Check]:
    provider = conversation.get("provider", DEFAULT_PROVIDER)
    if provider == "anthropic":
        return [_check_anthropic_key(conversation)]
    if provider == "ollama":
        checks = _check_ollama(conversation)
        # Only when the configured path is actually broken — see
        # _unused_anthropic_key_hint for why this isn't reported otherwise.
        if all(check.ok for check in checks):
            return checks
        return checks + _unused_anthropic_key_hint(conversation)
    return [
        Check(
            "provider",
            False,
            f"unknown provider {provider!r}",
            fix='Set conversation.provider in app/config.yaml to "ollama" or "anthropic".',
        )
    ]


def _api_key_env(conversation: dict) -> str:
    """Which environment variable holds the Anthropic key, per config."""
    return conversation.get("anthropic", {}).get("api_key_env", "ANTHROPIC_API_KEY")


def _unused_anthropic_key_hint(conversation: dict) -> list[Check]:
    """The dead end this module exists to remove, in its last remaining form.

    Setting a key is the documented way to use Anthropic, but it is not
    sufficient on its own: `conversation.provider` still decides who gets
    called, and it ships as "ollama". Someone who has a key and no Ollama
    therefore follows the README, gets "model server is not reachable", and
    is pointed at installing the very thing they were trying to avoid —
    while the key they did set goes unmentioned.

    Reported only when an Ollama check has already failed. A key set for
    some other tool entirely is common and none of this app's business; it
    becomes worth raising exactly when the configured provider can't serve
    the request and this one could.
    """
    key_env = _api_key_env(conversation)
    if not os.environ.get(key_env):
        return []
    return [
        Check(
            "anthropic key",
            False,
            f'{key_env} is set, but conversation.provider is "ollama"',
            fix=(
                'Set conversation.provider to "anthropic" in app/config.yaml (or switch provider '
                "in the app's settings panel) to use that key instead of a local model. Ignore "
                "this if the key belongs to something else."
            ),
        )
    ]


def _check_anthropic_key(conversation: dict) -> Check:
    key_env = _api_key_env(conversation)
    if os.environ.get(key_env):
        return Check("api key", True, f"{key_env} is set")
    return Check(
        "api key",
        False,
        f"{key_env} is not set",
        fix=(
            f"Put {key_env}=sk-ant-... in a .env file at the repo root (see .env.example), "
            "or export it in your shell. Without it the diff still opens, but nothing narrates."
        ),
    )


def _check_ollama(conversation: dict) -> list[Check]:
    """Reachability and model presence, reported separately.

    Two checks rather than one because the fixes are completely different —
    starting a server versus pulling a model — and conflating them is
    exactly the confusion this module exists to remove.
    """
    settings = conversation.get("ollama", {})
    base_url = settings.get("base_url", "http://127.0.0.1:11434")
    wanted = settings.get("model", "")

    try:
        import requests

        response = requests.get(f"{base_url}/api/tags", timeout=_TIMEOUT_SECONDS)
        response.raise_for_status()
        installed = [m.get("name", "") for m in response.json().get("models", [])]
    # Catching everything is this module's whole contract, not an exception
    # to it - see the docstring: no check may raise. A failure to reach the
    # server, of any kind, is reported as "not reachable" and startup
    # carries on.
    except Exception as exc:  # noqa: BLE001
        return [
            Check(
                "model server",
                False,
                f"{base_url} is not reachable ({type(exc).__name__})",
                fix=(
                    "Start Ollama (`ollama serve`, or launch the desktop app) and reload the page. "
                    "The diff still opens without it, but nothing will narrate."
                ),
            )
        ]

    server = Check("model server", True, f"{base_url} responded, {len(installed)} model(s) installed")
    if not wanted:
        return [
            server,
            Check(
                "model",
                False,
                "no model configured",
                fix="Set conversation.ollama.model in app/config.yaml.",
            ),
        ]
    # Exact match first, then the bare name: Ollama reports "name:tag" and a
    # config that omits the tag is a normal thing to write.
    if wanted in installed or any(name.split(":")[0] == wanted.split(":")[0] for name in installed):
        return [server, Check("model", True, f"{wanted} is installed")]
    return [
        server,
        Check(
            "model",
            False,
            f"{wanted} is not installed",
            fix=f"Run: ollama pull {wanted}" + (f"  (installed: {', '.join(installed)})" if installed else ""),
        ),
    ]


def format_report(checks: list[Check]) -> str:
    """A plain-text block for the terminal. ASCII only, deliberately: this
    is the first thing a user sees, and a mojibake tick on a Windows console
    reads as the app already being broken."""
    lines = []
    for check in checks:
        mark = "OK  " if check.ok else ("FAIL" if check.fatal else "WARN")
        lines.append(f"  [{mark}] {check.name}: {check.detail}")
        if check.fix:
            lines.append(f"         -> {check.fix}")
    return "\n".join(lines)


def has_fatal(checks: list[Check]) -> bool:
    return any(check.fatal and not check.ok for check in checks)
