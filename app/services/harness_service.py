"""Act Now through a coding agent the reviewer already uses (Cline, over ACP —
see acp_client.py), instead of a single model call from this app.

Owns: which agents are supported and whether one is ready, the permission
policy for a turn, and turning the agent's work into proposed changes the
reviewer previews before anything is written.

Must never write to the reviewed repo except in apply_changes, and only with
exactly what was previewed. The agent works in a throwaway copy of the repo
(tracked + untracked, non-ignored files), not the repo itself. Nothing in ACP
obliges an agent to route its edits through the client or to ask before
writing, so a permission-based preview would only hold for well-behaved
agents; a copy holds for all of them, and lets one change span several files.
The cost is the copy, bounded by _MAX_SANDBOX_FILES/_MAX_SANDBOX_BYTES.

Also owns "Look deeper" (run_agent_research): the same agent, read-only, on a
copy that carries git history, answering a reviewer's question instead of
editing. It must never be able to write anything, even to the copy.

The model is whatever the reviewer set up in Cline, read from Cline's own
settings (see resolve_agent_env) — ACP sessions don't read those themselves.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from .acp_client import (
    PROTOCOL_VERSION,
    AcpCancelled,
    AcpConnection,
    AcpError,
    AcpRemoteError,
    resolve_command,
)
from .editor_service import code_fence, resolve_within_repo
from .errors import ServiceError

log = logging.getLogger(__name__)

DEFAULT_AGENT = "none"
AGENTS: dict[str, dict] = {
    "cline": {
        "label": "Cline",
        "command": ["cline", "--acp"],
        "install_hint": "Install it with `npm i -g cline`, then sign in once with `cline auth`.",
    },
}

_DEFAULT_TIMEOUT_SECONDS = 600
# initialize/session/new: node startup plus the agent loading its config.
_SETUP_TIMEOUT_SECONDS = 60
# Big enough for real projects, small enough that a repo with a vendored
# dependency tree fails with a clear message rather than a long silent copy.
_MAX_SANDBOX_FILES = 20_000
_MAX_SANDBOX_BYTES = 200 * 1024 * 1024
# An agent answering a turn with commands or web fetches is outside what a
# previewable edit can show, and a command isn't confined to the copy.
_REJECTED_TOOL_KINDS = frozenset({"execute", "fetch"})
_INVALID_PARAMS = -32602
# The mode ids Cline uses for "make changes" and for read-only work. A session
# starts in "act" (measured on core 4.1.17), so research switches explicitly.
_EDIT_MODE = "act"
_RESEARCH_MODE = "plan"

# Look deeper has been measured with these models. research_note names them
# in the settings panel, so this is the one place to update after testing
# another model.
TESTED_RESEARCH_MODELS = ("claude-sonnet-5",)

# Local providers that need no key. Cline still refuses to start ANY session
# without CLINE_API_KEY (measured: "Authentication required" for ollama with no
# key, a session with a placeholder), so these get one.
_KEYLESS_PROVIDERS = frozenset({"ollama", "lmstudio"})
_KEYLESS_PLACEHOLDER = "no-key-needed"

# Research permission policy. Commands are allowed only when every segment of
# a chain is a read-only git command or a filter that reads stdin alone —
# measured: Cline writes `git log ...; git show ... | head -100` naturally, and
# a one-command rule refused most of its history lookups.
_GIT_READ_SUBCOMMANDS = frozenset({"log", "show", "blame", "diff", "grep", "status", "ls-files", "rev-parse"})
# Options that write a file, run a program, or read outside the repository.
_UNSAFE_GIT_OPTION_PREFIXES = (
    "--output",
    "--ext-diff",
    "--textconv",
    "--no-index",
    "--contents",
    "--open-files-in-pager",
    "-O",
)
_STDIN_FILTER = re.compile(r"^(head|tail|wc|sort|uniq)(\s+-{1,2}[\w-]+(=?\d+)?|\s+\d+)*$")
_SHELL_FORBIDDEN = re.compile(r"[<>`$\n\r]|(^|[^&])&($|[^&])")
_RESEARCH_READ_KINDS = frozenset({"read", "search"})
# Measured failure: after a refused command Cline sometimes ends its turn by
# asking "could you confirm...?" — a dead end behind a button. One follow-up.
_ASKS_FOR_INPUT = re.compile(
    r"(\?\s*$|could you (confirm|clarify|let me know)|would you like me to|shall i |let me know (if|whether|how))",
    re.IGNORECASE,
)
_CONTINUE_PROMPT = (
    "No one will reply during this investigation. Continue with what you're allowed to do — reading files, "
    "searching, read-only git — and give your best answer now. Say plainly what you couldn't establish."
)


class HarnessError(ServiceError):
    """The agent couldn't run the change, or its result can't be previewed or applied."""


@dataclass(frozen=True)
class HarnessStatus:
    agent: str
    available: bool
    detail: str


@dataclass(frozen=True)
class ProposedChange:
    """One file the agent proposes to change.

    old_text None means it created the file; new_text None means it deleted
    it."""

    file_path: str
    old_text: str | None
    new_text: str | None


@dataclass(frozen=True)
class AgentEdit:
    changes: tuple[ProposedChange, ...]
    summary: str


@dataclass(frozen=True)
class ActNowRequest:
    """What an Act Now preview was asked for: the location it concerns and
    each instruction in order — the first, then every refinement typed into
    the preview bar."""

    file_path: str
    where: str
    snippet: str
    instructions: tuple[str, ...]


@dataclass(frozen=True)
class AgentModel:
    """What the agent will run on, as far as this app can tell.

    source is where provider/model came from: "config.yaml" (an explicit
    harness.<agent>.env pin), "Cline settings", or "none" when nothing is set.
    has_key never carries the key itself — only whether one will be passed."""

    provider: str | None
    model: str | None
    source: str
    has_key: bool


@dataclass(frozen=True)
class ResearchResult:
    answer: str
    model: AgentModel
    tool_calls: int
    refused: int


def configured_agent(harness_config: dict) -> str:
    return harness_config.get("agent") or DEFAULT_AGENT


def agent_label(harness_config: dict) -> str:
    agent = configured_agent(harness_config)
    return AGENTS.get(agent, {}).get("label", agent)


def _agent_command(harness_config: dict) -> list[str] | None:
    agent = configured_agent(harness_config)
    if agent not in AGENTS:
        return None
    return (harness_config.get(agent) or {}).get("command") or AGENTS[agent]["command"]


def _cline_settings_path() -> Path:
    """Where Cline keeps each provider's saved settings. CLINE_PROVIDER_SETTINGS_PATH
    is Cline's own override for the same file, honoured so both agree."""
    override = os.environ.get("CLINE_PROVIDER_SETTINGS_PATH", "").strip()
    return Path(override) if override else Path.home() / ".cline" / "data" / "settings" / "providers.json"


def _saved_cline_settings() -> tuple[str | None, dict]:
    """(last used provider, {provider id: settings}) from Cline's own file, or
    (None, {}) when it's missing or not in the shape expected. It's Cline's
    internal file, so a format change must degrade to "not set up", never raise."""
    try:
        data = json.loads(_cline_settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, {}
    if not isinstance(data, dict) or not isinstance(data.get("providers"), dict):
        return None, {}
    settings = {
        name: entry["settings"]
        for name, entry in data["providers"].items()
        if isinstance(name, str) and isinstance(entry, dict) and isinstance(entry.get("settings"), dict)
    }
    last = data.get("lastUsedProvider")
    return (last if isinstance(last, str) and last else None), settings


def _saved_key(settings: dict) -> str | None:
    """The same order Cline itself reads a provider's credential in: a sign-in
    access token, then a saved API key, then one nested under auth."""
    auth = settings.get("auth")
    if not isinstance(auth, dict):
        auth = {}
    for value in (auth.get("accessToken"), settings.get("apiKey"), auth.get("apiKey")):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def resolve_agent_env(harness_config: dict) -> tuple[dict[str, str], AgentModel]:
    """The agent process's environment, and what it will run on.

    Cline's ACP sessions do NOT read the provider or model saved by
    `cline auth` — measured on core 4.1.17: with no CLINE_* variables a session
    refuses to start ("Authentication required"); with CLINE_PROVIDER but no
    CLINE_MODEL it silently takes the provider's first model (qwen2.5-coder,
    not the saved qwen3:8b); and it needs CLINE_API_KEY for every provider,
    even keyless ones. So this reads Cline's saved settings and passes all
    three, which makes "change the model in Cline" work here too.

    Precedence per variable: config.yaml's harness.<agent>.env, then this
    process's environment, then Cline's saved settings for the resolved
    provider. The key is passed only to the agent process — never logged and
    never returned (AgentModel.has_key is a boolean)."""
    agent = configured_agent(harness_config)
    pinned = {str(k): str(v) for k, v in ((harness_config.get(agent) or {}).get("env") or {}).items()}
    env = {**os.environ, **pinned}
    if agent != "cline":
        return env, AgentModel(None, None, "none", False)

    last_used, saved = _saved_cline_settings()
    provider = env.get("CLINE_PROVIDER") or last_used
    provider_settings = saved.get(provider, {}) if provider else {}
    model = env.get("CLINE_MODEL") or (
        provider_settings.get("model") if isinstance(provider_settings.get("model"), str) else None
    )
    key = env.get("CLINE_API_KEY") or _saved_key(provider_settings)
    if not key and provider in _KEYLESS_PROVIDERS:
        key = _KEYLESS_PLACEHOLDER

    if provider:
        env["CLINE_PROVIDER"] = provider
    if model:
        env["CLINE_MODEL"] = model
    if key:
        env["CLINE_API_KEY"] = key
    source = "config.yaml" if "CLINE_PROVIDER" in pinned else ("Cline settings" if provider else "none")
    return env, AgentModel(provider, model, source, bool(key))


def agent_model(harness_config: dict) -> AgentModel:
    return resolve_agent_env(harness_config)[1]


def research_note() -> str:
    """Guidance shown where the reviewer chooses a model (the settings panel),
    not under each answer: this is a bring-your-own-model app, so warning on
    every answer from a model nobody here tested would flag most answers for
    most users. Built from TESTED_RESEARCH_MODELS so the claim can't drift
    from what was measured."""
    tested = " and ".join(TESTED_RESEARCH_MODELS)
    return (
        f"Look deeper was tested with {tested} (through Cline) and works with any model you set up in Cline. "
        "Smaller models are weaker at exploring a codebase, so expect shallower answers from them."
    )


def harness_status(harness_config: dict) -> HarnessStatus:
    """Cheap (a PATH lookup and one small settings file), so it's safe to call
    per connection and after every settings save. Catches the two set-up
    problems that would otherwise surface only mid-review as a failed
    session: no provider chosen in Cline, and no key for a provider that
    needs one."""
    agent = configured_agent(harness_config)
    if agent == DEFAULT_AGENT:
        return HarnessStatus(agent, False, "Act Now needs a coding agent — choose Cline in Model settings.")
    if agent not in AGENTS:
        return HarnessStatus(agent, False, f"Unknown coding agent {agent!r} — choose Cline in Model settings.")
    command = _agent_command(harness_config)
    label = AGENTS[agent]["label"]
    if resolve_command(command) is None:
        return HarnessStatus(agent, False, f"{label} isn't installed. {AGENTS[agent]['install_hint']}")
    if agent == "cline" and _uses_real_cline(command):
        model = agent_model(harness_config)
        if not model.provider:
            return HarnessStatus(agent, False, "Cline has no provider set up yet — run `cline auth` once, then reload.")
        if not model.has_key:
            return HarnessStatus(
                agent,
                False,
                f"Cline has no API key saved for {model.provider} — run `cline auth -p {model.provider} -k <key>`, then reload.",
            )
    return HarnessStatus(agent, True, f"Act Now runs through {label}.")


def _uses_real_cline(command: list[str] | None) -> bool:
    """False for a stand-in agent configured as the command (tests, qa_agent's
    fake_acp_agent.py), which needs no provider or key."""
    if not command:
        return False
    return Path(command[0]).stem.lower() == "cline"


def build_act_now_prompt(request: ActNowRequest) -> str:
    """The prompt for one Act Now turn. A refinement is a fresh agent session
    too, so it carries the whole request: the first instruction, the edit
    already sitting in the copy (see run_agent_edit's base), and what the
    reviewer asked for after seeing it."""
    first, *refinements = request.instructions
    fence = code_fence(request.snippet)
    prompt = (
        "A code reviewer, looking at the uncommitted changes in this repository, asked for a "
        f"small change to be made now:\n\n{first}\n\n"
        f"It concerns {request.file_path} — {request.where}:\n{fence}\n{request.snippet}\n{fence}\n\n"
    )
    if refinements:
        asked = "\n".join(f"- {text}" for text in refinements)
        prompt += (
            "A change for this has already been made — it's in the files now. The reviewer looked at "
            f"it and asked for more (oldest first; the last is the newest):\n\n{asked}\n\n"
            "Adjust the files so they do what the reviewer now wants: build on the earlier edit, or "
            "undo parts of it. "
        )
    return prompt + (
        "Edit only what this change needs. Don't run commands, install anything or commit: "
        "the reviewer sees the resulting diff and decides whether to apply it. When you're done, "
        "reply with one or two sentences saying what you changed."
    )


def run_agent_edit(
    harness_config: dict,
    repo_path: str,
    prompt_text: str,
    cancel: threading.Event,
    base: tuple[ProposedChange, ...] = (),
) -> AgentEdit:
    """Runs one agent turn in a copy of the repo and returns what it changed.
    Blocking — call through app/web/runtime.py's run_agent.

    base is a previewed change to start from (a refinement): it's written into
    the copy before the agent runs, and the result is still measured against
    the repo itself, so it replaces that preview rather than adding to it."""
    label = agent_label(harness_config)
    with tempfile.TemporaryDirectory(prefix="act_now_", ignore_cleanup_errors=True) as tmp:
        sandbox = Path(tmp).resolve()
        snapshot = _build_sandbox(repo_path, sandbox)
        _overlay(repo_path, sandbox, base)
        turn = _Turn(sandbox)
        result = _run_session(harness_config, sandbox, turn, _EDIT_MODE, prompt_text, cancel, write=True)

        stop_reason = result.get("stopReason")
        if stop_reason == "refusal":
            raise HarnessError(f"{label} declined to make this change. {turn.summary()}".strip())
        if stop_reason == "cancelled":
            raise HarnessError(f"{label} stopped before finishing")
        changes = _collect_changes(repo_path, sandbox, snapshot)
        if stop_reason not in (None, "end_turn") and changes:
            log.info("agent stopped with %s; offering its partial change for preview", stop_reason)
        return AgentEdit(tuple(changes), turn.summary())


def apply_changes(repo_path: str, changes: tuple[ProposedChange, ...]) -> None:
    """Writes exactly the previewed changes, or nothing: every file must still
    be what the agent started from, so an edit made in the meantime (by the
    reviewer, or another tool) is never overwritten."""
    targets: list[tuple[ProposedChange, Path]] = []
    conflicts: list[str] = []
    for change in changes:
        target = resolve_within_repo(repo_path, change.file_path)
        if target is None:
            raise HarnessError(f"Refusing to write outside the repository: {change.file_path}")
        if _read_text(target) != change.old_text:
            conflicts.append(change.file_path)
        targets.append((change, target))
    if conflicts:
        raise HarnessError(f"Not applied — changed since the preview: {', '.join(conflicts)}. Run Act Now again.")
    for change, target in targets:
        if change.new_text is None:
            target.unlink(missing_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(change.new_text.encode("utf-8"))


def build_research_prompt(file_path: str, header: str, diff: str, question: str, root: Path) -> str:
    """Builds the prompt for one read-only Look deeper investigation.

    The last two lines of instruction exist because of measured failures:
    without the absolute root Cline lost track of its working directory
    once, and after a refused command it twice stopped to ask for
    confirmation."""
    fence = code_fence(diff)
    return (
        "You are helping a code reviewer who is looking at one hunk of an uncommitted change in this "
        "repository. You can read files, search the code, and run read-only git commands (git log, git show, "
        "git blame, git diff, git grep, git status, git ls-files, git rev-parse). You cannot edit anything or "
        "run any other command — those are refused.\n\n"
        f"The hunk under review ({file_path}, {header}):\n{fence}diff\n{diff}\n{fence}\n\n"
        f"The reviewer asks: {question}\n\n"
        "Investigate as much as you need, then answer in a short paragraph or two, as plainly as you can. Name "
        "the files, functions or commits your answer rests on. If you couldn't establish something, say so "
        "rather than guessing. Start your final answer with a line reading exactly: ## Answer\n\n"
        f"The repository root is {root} — use paths relative to it, or absolute paths under it. No one will "
        "reply during this investigation: never stop to ask a question or for confirmation. If a command is "
        "refused, continue with file reads and search instead."
    )


def run_agent_research(
    harness_config: dict,
    repo_path: str,
    file_path: str,
    header: str,
    diff: str,
    question: str,
    cancel: threading.Event,
) -> ResearchResult:
    """Look deeper: one read-only investigation of a reviewer's question.
    Blocking — call through app/web/runtime.py's run_agent.

    Read-only at every layer, not just by instruction: the session runs in
    Cline's "plan" mode, the client advertises no file-write capability and
    refuses fs/write_text_file, and the permission policy allows only reads,
    searches and read-only git (see _Turn). The copy carries history, so
    "why" and "what changed before" questions can reach the log."""
    label = agent_label(harness_config)
    model = agent_model(harness_config)
    with tempfile.TemporaryDirectory(prefix="look_deeper_", ignore_cleanup_errors=True) as tmp:
        sandbox = Path(tmp).resolve()
        _build_research_sandbox(repo_path, sandbox)
        turn = _Turn(sandbox, research=True)
        continued_from: list[int] = []

        def follow_up(result: dict) -> str | None:
            # At most once, and only when the turn ended normally with the
            # agent waiting for an answer that will never come.
            if continued_from or result.get("stopReason") not in (None, "end_turn"):
                return None
            if _ASKS_FOR_INPUT.search(turn.summary()[-400:]):
                continued_from.append(turn.mark())
                log.info("look deeper: agent asked for input; continuing once")
                return _CONTINUE_PROMPT
            return None

        prompt_text = build_research_prompt(file_path, header, diff, question, sandbox)
        result = _run_session(
            harness_config, sandbox, turn, _RESEARCH_MODE, prompt_text, cancel, write=False, follow_up=follow_up
        )
        stop_reason = result.get("stopReason")
        if stop_reason == "refusal":
            raise HarnessError(f"{label} declined to look into this. {turn.summary()}".strip())
        if stop_reason == "cancelled":
            raise HarnessError(f"{label} stopped before finishing")
        text = turn.text_since(continued_from[0]) if continued_from else turn.summary()
        answer = _final_answer(text)
        if not answer:
            raise HarnessError(f"{label} finished without an answer")
        return ResearchResult(answer, model, turn.tool_call_count, turn.refused_count)


def _run_session(
    harness_config: dict,
    sandbox: Path,
    turn: _Turn,
    mode: str,
    prompt_text: str,
    cancel: threading.Event,
    *,
    write: bool,
    follow_up=None,
) -> dict:
    """One agent process, one session, one prompt — plus at most one
    follow_up(result) prompt when that callback returns text. Each prompt is
    bounded by harness.timeout_seconds."""
    command = _agent_command(harness_config)
    if command is None:
        raise HarnessError(harness_status(harness_config).detail)
    label = agent_label(harness_config)
    timeout = harness_config.get("timeout_seconds") or _DEFAULT_TIMEOUT_SECONDS
    env, _ = resolve_agent_env(harness_config)
    try:
        with AcpConnection(command, str(sandbox), turn.on_request, turn.on_notification, env=env) as conn:
            conn.request("initialize", _initialize_params(write), _SETUP_TIMEOUT_SECONDS, cancel)
            try:
                session = conn.request(
                    "session/new", {"cwd": str(sandbox), "mcpServers": []}, _SETUP_TIMEOUT_SECONDS, cancel
                )
            except AcpCancelled:
                raise
            except AcpError as exc:
                hint = AGENTS.get(configured_agent(harness_config), {}).get("install_hint", "")
                raise HarnessError(f"{label} couldn't start a session ({exc}). {hint}") from exc
            session_id = session.get("sessionId")
            if not session_id:
                raise HarnessError(f"{label} started a session without an id")
            _switch_mode(conn, session, session_id, mode, cancel)
            result = _prompt(conn, session_id, prompt_text, timeout, cancel)
            follow = follow_up(result) if follow_up else None
            if follow:
                result = _prompt(conn, session_id, follow, timeout, cancel)
            return result
    except AcpCancelled as exc:
        raise HarnessError(f"{label} was cancelled") from exc
    except AcpError as exc:
        raise HarnessError(f"{label} failed: {exc}") from exc


def _prompt(conn: AcpConnection, session_id: str, text: str, timeout: float, cancel: threading.Event) -> dict:
    prompt = {"sessionId": session_id, "prompt": [{"type": "text", "text": text}]}
    try:
        return conn.request("session/prompt", prompt, timeout, cancel)
    except AcpCancelled:
        try:
            conn.notify("session/cancel", {"sessionId": session_id})
        except AcpError:
            pass
        raise


def _switch_mode(conn: AcpConnection, session: dict, session_id: str, mode: str, cancel: threading.Event) -> None:
    """Agents with modes may start a session in the wrong one — Cline offers
    "plan" (read-only) and "act", and starts in "act". Act Now needs "act";
    Look deeper needs "plan"."""
    modes = session.get("modes") or {}
    available = {m.get("id") for m in modes.get("availableModes") or [] if isinstance(m, dict)}
    if mode in available and modes.get("currentModeId") != mode:
        conn.request("session/set_mode", {"sessionId": session_id, "modeId": mode}, _SETUP_TIMEOUT_SECONDS, cancel)


def _initialize_params(write: bool = True) -> dict:
    return {
        "protocolVersion": PROTOCOL_VERSION,
        # fs lets an agent that prefers client file access edit the copy
        # through us; terminal stays off, same reason as _REJECTED_TOOL_KINDS.
        # Look deeper advertises no write capability at all.
        "clientCapabilities": {"fs": {"readTextFile": True, "writeTextFile": write}, "terminal": False},
        "clientInfo": {"name": "ai-pear-review", "title": "AI Pear Review", "version": "0"},
    }


def _final_answer(text: str) -> str:
    """The part after the last "## Answer" line the prompt asks for, dropping
    the agent's narration of its own steps; the whole text if it didn't use one."""
    matches = list(re.finditer(r"^\s*##\s*Answer\s*$", text, re.MULTILINE))
    return (text[matches[-1].end() :] if matches else text).strip()


def _build_research_sandbox(repo_path: str, sandbox: Path) -> None:
    """A copy that carries history: a local clone (objects hardlinked, so it's
    cheap even for a large repo, and nothing is written to the reviewer's
    repository), the working files copied over it exactly as Act Now copies
    them, and an index reset to HEAD — so `git diff` in the copy shows the
    change under review, staged and unstaged together, as the review does."""
    repo = str(Path(repo_path).resolve())
    _git_lines(repo, ["clone", "--local", "--no-checkout", "--quiet", repo, str(sandbox)])
    _build_sandbox(repo_path, sandbox)
    _git_lines(str(sandbox), ["read-tree", "HEAD"])


def is_read_only_command(command: str, root: Path) -> bool:
    """Whether a shell command Look deeper's agent asked to run is safe: every
    segment of a chain (; && || |) is a read-only git command or a filter that
    only reads stdin. Verified against the commands Cline actually sent while
    this was measured, and against hostile ones (redirection, substitution,
    chaining into another program, reading outside the repo)."""
    command = command.strip()
    if not command or _SHELL_FORBIDDEN.search(command):
        return False
    segments = [segment.strip() for segment in re.split(r"\|\||&&|;|\|", command)]
    if not segments[0].startswith("git"):
        return False
    for segment in segments:
        if segment.startswith("git"):
            if not _is_read_only_git(segment, root):
                return False
        elif not _STDIN_FILTER.match(segment):
            return False
    return True


def _is_read_only_git(segment: str, root: Path) -> bool:
    try:
        tokens = [token.strip("\"'") for token in shlex.split(segment, posix=False)]
    except ValueError:
        return False
    if not tokens or tokens[0] != "git":
        return False
    i = 1
    # Global options before the subcommand: only -C pointing at the copy, and
    # --no-pager. Anything else there (notably -c) could reconfigure git.
    while i < len(tokens) and tokens[i] in ("-C", "--no-pager", "-P"):
        if tokens[i] == "-C":
            if i + 1 >= len(tokens) or not _is_root(tokens[i + 1], root):
                return False
            i += 2
        else:
            i += 1
    if i >= len(tokens) or tokens[i] not in _GIT_READ_SUBCOMMANDS:
        return False
    return not any(token.startswith(_UNSAFE_GIT_OPTION_PREFIXES) for token in tokens[i + 1 :])


def _is_root(value: str, root: Path) -> bool:
    """ "." or the copy's own absolute path — measured: Cline habitually writes
    `git -C <absolute path of its working directory> ...`, and refusing that
    was the cause of both failed investigations."""
    if value == ".":
        return True
    try:
        return os.path.samefile(value, root)
    except OSError:
        return False


def _tool_commands(tool: dict) -> list[str] | None:
    raw_input = tool.get("rawInput")
    if isinstance(raw_input, dict):
        if isinstance(raw_input.get("commands"), list):
            return [str(command) for command in raw_input["commands"]]
        if isinstance(raw_input.get("command"), str):
            return [raw_input["command"]]
    title = tool.get("title") or ""
    if title.startswith("run_commands: "):
        return [title[len("run_commands: ") :]]
    return None


class _Turn:
    """Answers the agent's requests during one prompt turn and gathers its reply.

    research=True is Look deeper's policy: reads and searches inside the copy,
    read-only git commands (is_read_only_command), and nothing else — no
    edits, and no client file writes even into the copy."""

    def __init__(self, sandbox: Path, research: bool = False) -> None:
        self._sandbox = sandbox
        self._research = research
        self._message_parts: list[str] = []
        # Permission requests may name a tool call only by id, with its kind
        # and paths sent earlier in session/update notifications.
        self._tool_calls: dict[str, dict] = {}
        self._refused = 0
        self._lock = threading.Lock()

    def summary(self) -> str:
        with self._lock:
            return "".join(self._message_parts).strip()

    def mark(self) -> int:
        """A position in the reply, for text_since — so a follow-up prompt's
        answer can be read without the turn before it."""
        with self._lock:
            return len(self._message_parts)

    def text_since(self, mark: int) -> str:
        with self._lock:
            return "".join(self._message_parts[mark:]).strip()

    @property
    def tool_call_count(self) -> int:
        with self._lock:
            return len(self._tool_calls)

    @property
    def refused_count(self) -> int:
        with self._lock:
            return self._refused

    def on_notification(self, method: str, params: dict) -> None:
        if method != "session/update":
            return
        update = params.get("update") or {}
        kind = update.get("sessionUpdate")
        if kind == "agent_message_chunk":
            content = update.get("content") or {}
            if content.get("type") == "text":
                with self._lock:
                    self._message_parts.append(content.get("text", ""))
        elif kind in ("tool_call", "tool_call_update"):
            self._remember_tool_call(update)

    def on_request(self, method: str, params: dict) -> object:
        if method == "session/request_permission":
            return self._permission(params)
        if method == "fs/read_text_file":
            return {"content": self._read(params)}
        if method == "fs/write_text_file":
            if self._research:
                raise AcpRemoteError(_INVALID_PARAMS, "Look deeper is read-only: nothing can be written")
            path = self._sandbox_path(params.get("path"))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(str(params.get("content", "")).encode("utf-8"))
            return None
        raise AcpRemoteError(-32601, f"{method} is not supported by this client")

    def _remember_tool_call(self, update: dict) -> None:
        call_id = update.get("toolCallId")
        if not call_id:
            return
        with self._lock:
            merged = self._tool_calls.setdefault(call_id, {})
            merged.update({k: v for k, v in update.items() if v is not None})

    def _permission(self, params: dict) -> dict:
        tool = dict(params.get("toolCall") or {})
        with self._lock:
            tool = {**self._tool_calls.get(tool.get("toolCallId") or "", {}), **tool}
        kind = tool.get("kind") or "other"
        paths = _tool_paths(tool)
        inside = all(self._is_inside(p) for p in paths)
        if self._research:
            if kind in _RESEARCH_READ_KINDS:
                allowed = inside
            elif kind == "execute":
                commands = _tool_commands(tool)
                allowed = all(is_read_only_command(c, self._sandbox) for c in commands) if commands else False
            else:
                allowed = False
        else:
            # "other" says nothing about what the tool does, so only a call that
            # names its files (all inside the copy) gets the benefit of the doubt.
            allowed = kind not in _REJECTED_TOOL_KINDS and inside and (kind != "other" or bool(paths))
        if not allowed:
            with self._lock:
                self._refused += 1
        log.info(
            "acp permission: %s %r paths=%s -> %s", kind, tool.get("title"), paths, "allow" if allowed else "reject"
        )
        return _pick_option(params.get("options") or [], allowed)

    def _read(self, params: dict) -> str:
        text = _read_text(self._sandbox_path(params.get("path")))
        if text is None:
            raise AcpRemoteError(_INVALID_PARAMS, f"cannot read {params.get('path')}")
        lines = text.splitlines(keepends=True)
        start = max(int(params.get("line") or 1) - 1, 0)
        limit = params.get("limit")
        end = start + int(limit) if limit else len(lines)
        return "".join(lines[start:end])

    def _sandbox_path(self, raw: object) -> Path:
        if not isinstance(raw, str) or not self._is_inside(raw):
            raise AcpRemoteError(_INVALID_PARAMS, f"path is outside the workspace: {raw}")
        return self._resolve(raw)

    def _resolve(self, raw: str) -> Path:
        path = Path(raw)
        return (path if path.is_absolute() else self._sandbox / path).resolve()

    def _is_inside(self, raw: str) -> bool:
        return self._resolve(raw).is_relative_to(self._sandbox)


def _tool_paths(tool: dict) -> list[str]:
    paths = [loc.get("path") for loc in tool.get("locations") or [] if isinstance(loc, dict)]
    paths += [
        item.get("path") for item in tool.get("content") or [] if isinstance(item, dict) and item.get("type") == "diff"
    ]
    raw_input = tool.get("rawInput")
    if isinstance(raw_input, dict):
        paths += [raw_input.get(key) for key in ("path", "file_path", "filePath", "abs_path")]
    return [p for p in paths if isinstance(p, str) and p]


def _pick_option(options: list[dict], allow: bool) -> dict:
    wanted = ("allow_once", "allow_always") if allow else ("reject_once", "reject_always")
    for kind in wanted:
        for option in options:
            if option.get("kind") == kind and option.get("optionId"):
                return {"outcome": {"outcome": "selected", "optionId": option["optionId"]}}
    return {"outcome": {"outcome": "cancelled"}}


def _read_text(path: Path) -> str | None:
    """None for a missing file. Raises for a binary one: the preview can only
    show text, and a change nobody can see must not be applied."""
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise HarnessError(f"could not read {path.name}: {exc}") from exc
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HarnessError(f"{path.name} isn't a text file — Act Now only previews text changes") from exc


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git_lines(repo_path: str, args: list[str], stdin: str | None = None) -> list[str]:
    try:
        result = subprocess.run(
            ["git", "-C", repo_path, *args],
            input=stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HarnessError(f"git {args[0]} failed: {exc}") from exc
    if result.returncode not in (0, 1):  # check-ignore exits 1 for "nothing ignored"
        raise HarnessError(f"git {args[0]} failed: {result.stderr.strip()}")
    return [p for p in result.stdout.split("\0") if p]


def _build_sandbox(repo_path: str, sandbox: Path) -> dict[str, str]:
    """Copies the repo's tracked and untracked, non-ignored files into sandbox
    and returns {relative path: sha256} for each, to compare against later."""
    repo = Path(repo_path).resolve()
    files = _git_lines(repo_path, ["ls-files", "-z", "--cached", "--others", "--exclude-standard"])
    if len(files) > _MAX_SANDBOX_FILES:
        raise HarnessError(
            f"This repo has {len(files)} files, over Act Now's {_MAX_SANDBOX_FILES}-file limit for the agent's working copy."
        )
    snapshot: dict[str, str] = {}
    total = 0
    for rel in dict.fromkeys(files):  # --cached and --others can overlap during a merge
        source = repo / rel
        if not source.is_file():  # deleted in the working tree, or a submodule
            continue
        total += source.stat().st_size
        if total > _MAX_SANDBOX_BYTES:
            raise HarnessError(
                f"This repo is over Act Now's {_MAX_SANDBOX_BYTES // (1024 * 1024)} MB limit for the agent's working copy."
            )
        target = sandbox / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        snapshot[rel] = _digest(source)
    return snapshot


def _overlay(repo_path: str, sandbox: Path, base: tuple[ProposedChange, ...]) -> None:
    """Writes a previewed change into the copy. Refuses, like apply_changes,
    if the repo no longer matches what that preview started from — the
    refined result would otherwise carry the preview's stale text over a
    file the reviewer has since edited."""
    for change in base:
        original = resolve_within_repo(repo_path, change.file_path)
        target = resolve_within_repo(str(sandbox), change.file_path)
        if original is None or target is None:
            raise HarnessError(f"Refusing to write outside the repository: {change.file_path}")
        if _read_text(original) != change.old_text:
            raise HarnessError(f"{change.file_path} changed since the preview — run Act Now again.")
        if change.new_text is None:
            target.unlink(missing_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(change.new_text.encode("utf-8"))


def _collect_changes(repo_path: str, sandbox: Path, snapshot: dict[str, str]) -> list[ProposedChange]:
    repo = Path(repo_path).resolve()
    present: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(sandbox):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for name in filenames:
            present.add((Path(dirpath) / name).relative_to(sandbox).as_posix())

    changes: list[ProposedChange] = []
    for rel, digest in sorted(snapshot.items()):
        copied = sandbox / rel
        if rel in present and _digest(copied) == digest:
            continue
        original = repo / rel
        if not original.is_file() or _digest(original) != digest:
            raise HarnessError(f"{rel} changed in the repo while the agent was working — run Act Now again.")
        new_text = _read_text(copied) if rel in present else None
        changes.append(ProposedChange(rel, _read_text(original), new_text))

    created = sorted(present - snapshot.keys())
    ignored = set(_git_lines(repo_path, ["check-ignore", "-z", "--stdin"], "\0".join(created))) if created else set()
    for rel in created:
        if rel in ignored:
            continue  # build output or caches, not part of the change
        if (repo / rel).exists():
            raise HarnessError(f"{rel} appeared in the repo while the agent was working — run Act Now again.")
        changes.append(ProposedChange(rel, None, _read_text(sandbox / rel)))
    return changes
