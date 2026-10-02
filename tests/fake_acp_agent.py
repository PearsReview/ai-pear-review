"""A scripted ACP agent for tests/test_harness_service.py — speaks just enough
of the protocol over stdio to exercise acp_client.py and harness_service.py
without Cline or a model.

    python fake_acp_agent.py <scenario>

Not a test module (no test_ prefix): pytest never collects it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

_next_id = 1000
_backlog: list[dict] = []
# Edit scenarios start in the read-only "plan" mode, so Act Now's switch to "act"
# is exercised; research scenarios start in "act" — which is where real Cline
# starts (measured on core 4.1.17) — so Look deeper's switch to "plan" is.
_mode = "plan"
_prompts = 0


def send(message: dict) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def read_message() -> dict | None:
    if _backlog:
        return _backlog.pop(0)
    line = sys.stdin.readline()
    return json.loads(line) if line else None


def call_client(method: str, params: dict) -> dict:
    """Sends a request to the client and waits for its answer, queueing
    anything else that arrives first."""
    global _next_id
    _next_id += 1
    my_id = _next_id
    send({"jsonrpc": "2.0", "id": my_id, "method": method, "params": params})
    while True:
        line = sys.stdin.readline()
        if not line:
            sys.exit(0)
        message = json.loads(line)
        if message.get("id") == my_id and "method" not in message:
            return message
        _backlog.append(message)


def update(session_id: str, body: dict) -> None:
    send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": session_id, "update": body}})


def say(session_id: str, text: str) -> None:
    update(session_id, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}})


_OPTIONS = [
    {"optionId": "yes", "name": "Allow", "kind": "allow_once"},
    {"optionId": "no", "name": "Reject", "kind": "reject_once"},
]


def ask(session_id: str, tool_call_id: str, kind: str, paths: list[str], raw_input: dict | None = None) -> str:
    """Announces a tool call, then asks permission naming it only by id."""
    body = {
        "sessionUpdate": "tool_call",
        "toolCallId": tool_call_id,
        "title": f"{kind} tool",
        "kind": kind,
        "status": "pending",
        "locations": [{"path": p} for p in paths],
    }
    if raw_input is not None:
        body["rawInput"] = raw_input
    update(session_id, body)
    reply = call_client(
        "session/request_permission",
        {
            "sessionId": session_id,
            "toolCall": {"toolCallId": tool_call_id},
            "options": _OPTIONS,
        },
    )
    outcome = reply["result"]["outcome"]
    return outcome.get("optionId", outcome["outcome"])


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True).stdout.strip()


def run_turn(scenario: str, session_id: str, cwd: Path) -> str:
    if scenario == "comment" and _mode == "plan":
        # qa_agent's server runs every agent turn through this one scenario, so
        # a read-only turn (Look deeper, which switches to plan) gets an answer
        # and an edit turn (Act Now, which switches to act) gets an edit.
        read = ask(session_id, "t1", "read", [str(cwd / "sample.py")])
        say(session_id, f"Checking sample.py first.\n## Answer\nsample.py was read (read={read}); nothing was changed.")
        return "end_turn"
    if scenario in ("edit", "comment", "fs_write") and _mode != "act":
        say(session_id, f"still in {_mode} mode, not editing")
        return "end_turn"
    if scenario.startswith("research") and _mode != "plan":
        say(session_id, f"## Answer\nstill in {_mode} mode")
        return "end_turn"
    if scenario == "research_git":
        # A read and a read-only git command, both allowed: answers from real
        # history in the copy, to prove the copy carries it.
        read = ask(session_id, "t1", "read", [str(cwd / "sample.py")])
        ran = ask(session_id, "t2", "execute", [], {"commands": [f"git -C {cwd} log --oneline -1"]})
        last = _git(cwd, "log", "--format=%s", "-1") if ran == "yes" else ""
        changed = _git(cwd, "diff", "--name-only", "HEAD") if ran == "yes" else ""
        say(session_id, "Let me look at the history first.\n")
        say(session_id, f"## Answer\nread={read} git={ran} last={last} changed={changed}")
    elif scenario == "research_forbidden":
        outcomes = [
            "edit=" + ask(session_id, "t1", "edit", [str(cwd / "sample.py")]),
            "redirect=" + ask(session_id, "t2", "execute", [], {"commands": ["git log > out.txt"]}),
            "other_program=" + ask(session_id, "t3", "execute", [], {"commands": ["git log; rm -rf ."]}),
            "fetch=" + ask(session_id, "t4", "fetch", []),
        ]
        reply = call_client(
            "fs/write_text_file", {"sessionId": session_id, "path": str(cwd / "sample.py"), "content": "x = 2\n"}
        )
        outcomes.append("fs_write=" + ("error" if "error" in reply else "ok"))
        say(session_id, "## Answer\n" + " ".join(outcomes))
    elif scenario == "research_asks":
        if _prompts == 1:
            say(session_id, "The command was refused. Could you confirm I may run git?")
        else:
            say(session_id, "## Answer\ncarried on without asking")
    elif scenario == "research_asks_twice":
        say(session_id, f"prompt {_prompts}: could you confirm I may run git?")
    if scenario == "env":
        say(session_id, os.environ.get("FAKE_AGENT_SETTING", "unset"))
    elif scenario == "edit":
        if ask(session_id, "t1", "edit", [str(cwd / "sample.py")]) == "yes":
            target = cwd / "sample.py"
            # Bytes, so Windows doesn't turn the newlines into CRLF behind the test's back.
            target.write_bytes(b"# greeting helpers\n" + target.read_bytes())
            (cwd / "new_module.py").write_bytes(b"VALUE = 1\n")
            (cwd / "notes.txt").unlink()
            (cwd / "build").mkdir(exist_ok=True)
            (cwd / "build" / "out.txt").write_text("generated\n", encoding="utf-8")
        say(session_id, "Added a comment.")
    elif scenario == "comment":
        # qa_agent's scenario (see qa_agent/conftest.py's app_server): one
        # plain, predictable edit to the file under review.
        if ask(session_id, "t1", "edit", [str(cwd / "sample.py")]) == "yes":
            target = cwd / "sample.py"
            target.write_bytes(b"# Reviewed with Act Now\n" + target.read_bytes())
        say(session_id, "Added a comment at the top of sample.py.")
    elif scenario == "fs_write":
        call_client(
            "fs/write_text_file", {"sessionId": session_id, "path": str(cwd / "sample.py"), "content": "x = 2\n"}
        )
        say(session_id, "Rewrote it.")
    elif scenario == "fs_outside":
        reply = call_client("fs/read_text_file", {"sessionId": session_id, "path": str(cwd.parent / "secret.txt")})
        say(session_id, "error" if "error" in reply else "read it")
    elif scenario == "execute":
        say(session_id, "outcome=" + ask(session_id, "t1", "execute", []))
    elif scenario == "outside":
        say(session_id, "outcome=" + ask(session_id, "t1", "edit", [str(cwd.parent / "elsewhere.py")]))
    elif scenario == "other_no_paths":
        say(session_id, "outcome=" + ask(session_id, "t1", "other", []))
    elif scenario == "hang":
        while True:
            message = read_message()
            if message is None or message.get("method") == "session/cancel":
                return "cancelled"
    elif scenario == "crash":
        sys.stderr.write("boom: something broke\n")
        sys.stderr.flush()
        sys.exit(3)
    elif scenario == "slow_exit":
        say(session_id, "done")
    return "end_turn"


def main() -> None:
    global _mode, _prompts
    scenario = sys.argv[1]
    if scenario.startswith("research"):
        _mode = "act"
    cwd = Path(os.getcwd())
    while True:
        message = read_message()
        if message is None:
            return
        method, msg_id, params = message.get("method"), message.get("id"), message.get("params") or {}
        if method == "initialize":
            send(
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {"protocolVersion": 1, "agentCapabilities": {}, "authMethods": []},
                }
            )
        elif method == "session/new":
            if scenario == "no_auth":
                send({"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32000, "message": "Authentication required"}})
            else:
                modes = {
                    "currentModeId": _mode,
                    "availableModes": [{"id": "plan", "name": "Plan"}, {"id": "act", "name": "Act"}],
                }
                send({"jsonrpc": "2.0", "id": msg_id, "result": {"sessionId": "sess_1", "modes": modes}})
        elif method == "session/set_mode":
            _mode = params["modeId"]
            send({"jsonrpc": "2.0", "id": msg_id, "result": {}})
        elif method == "session/prompt":
            _prompts += 1
            stop = run_turn(scenario, params["sessionId"], cwd)
            send({"jsonrpc": "2.0", "id": msg_id, "result": {"stopReason": stop}})
            if scenario == "slow_exit":
                time.sleep(30)  # ignores stdin closing, so close() must kill it


if __name__ == "__main__":
    main()
