"""Process-wide configuration, resolved once at import: config.yaml with the
UI-saved overrides layered on top (see settings_store.py), plus logging
setup and the non-loopback bind warning. Everything else reads CONFIG from
here; nothing in this module depends on a connection."""

from __future__ import annotations

import ipaddress
import logging
import os
from pathlib import Path

import yaml

from ..services.settings_store import SECTION_OVERRIDE_KEYS, apply_overrides, load_overrides

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("ai_pear_review")

APP_DIR = Path(__file__).resolve().parent.parent
BASE_DIR = APP_DIR.parent
STATIC_DIR = BASE_DIR / "static"


def load_config() -> dict:
    with open(APP_DIR / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


CONFIG = load_config()

# run.py's --repo, which has to reach this module as an environment variable
# (see REPO_PATH_ENV there): CONFIG is resolved at import, before anything
# could pass it an argument. Absent — the normal case when the app is
# launched from inside the repo being reviewed — config.yaml's own
# server.repo_path ("." by default) stands.
_repo_from_env = os.environ.get("REVIEW_REPO_PATH")
if _repo_from_env:
    CONFIG["server"]["repo_path"] = _repo_from_env

# A pull request review, started by the VS Code extension on a worktree of
# the PR's head: REVIEW_BASE_SHA is the merge-base to diff against, and
# REVIEW_READ_ONLY=1 turns off everything that writes (Act Now, the plan and
# skill files). Comments go to GitHub from the extension instead.
CONFIG["server"]["base_sha"] = os.environ.get("REVIEW_BASE_SHA") or None
CONFIG["server"]["read_only"] = os.environ.get("REVIEW_READ_ONLY") == "1"

# Layer any UI-saved model settings over config.yaml (see
# services/settings_store.py — config.yaml itself is never rewritten, so
# its comments survive). Done once here, at import: ConversationClient is
# built per WebSocket connection from CONFIG["conversation"], so a later
# in-memory change to that dict is picked up by the next connection
# without restarting the server — which is what makes the settings panel
# work at all (handle_set_settings below).
#
# The overrides FILE can also carry "tts"/"stt"/"harness" keys (see
# SECTION_OVERRIDE_KEYS) — excluded here before applying to CONFIG["conversation"],
# since apply_overrides' generic pass-through would otherwise merge them
# straight into the conversation config too (see that function's own
# docstring). Applied to CONFIG["tts"]/CONFIG["stt"] on the next lines
# instead — directly, not via apply_overrides, since each is already just
# one flat dict here (no provider sub-sections to merge one level into).
_startup_overrides = load_overrides(CONFIG["server"].get("repo_path", "."))
CONFIG["conversation"] = apply_overrides(
    CONFIG["conversation"],
    {k: v for k, v in _startup_overrides.items() if k not in SECTION_OVERRIDE_KEYS},
)
CONFIG["tts"] = {**CONFIG["tts"], **(_startup_overrides.get("tts") or {})}
CONFIG["stt"] = {**CONFIG["stt"], **(_startup_overrides.get("stt") or {})}
CONFIG["harness"] = {**(CONFIG.get("harness") or {}), **(_startup_overrides.get("harness") or {})}


def is_loopback_host(host: str) -> bool:
    """Whether this host names only the machine the process runs on.

    "127.0.0.1", "::1" and "localhost" do. Anything else — most commonly
    "0.0.0.0", which run.py's own comment invites for remote access —
    accepts connections from other machines on the network.

    That matters because this server's WebSocket protocol is
    unauthenticated. The Origin check in server.py guards against another
    browser tab on this machine, not against a different machine entirely,
    so binding non-loopback hands the file read and write surface Act Now
    exposes to anyone who can reach the port.

    Also used on the Host header of an incoming connection, not just on the
    configured bind address — see server.py's _origin_is_trusted."""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False  # not a parseable IP and not "localhost" — treat as non-loopback, the conservative default


BOUND_TO_LOOPBACK = is_loopback_host(CONFIG["server"].get("host", "127.0.0.1"))

if not BOUND_TO_LOOPBACK:
    log.warning(
        "server.host is %r, not loopback — this exposes the unauthenticated "
        "WebSocket protocol (including Act Now's file read/write) to anyone "
        "who can reach this port on the network, not just this machine.",
        CONFIG["server"].get("host"),
    )
