"""A minimal Agent Client Protocol (ACP) client: JSON-RPC 2.0 over an agent
subprocess's stdio, one JSON object per line. https://agentclientprotocol.com

Owns the transport only — spawning the agent, matching responses to
requests, and handing the agent's own requests and notifications to
callbacks. What to allow, what a turn means and where files live are
harness_service.py's. Never touches the repo itself.

Threads rather than asyncio: every long-running call in this app runs in a
worker thread with a threading.Event to cancel it (see app/web/runtime.py),
and asyncio subprocesses need the Proactor loop on Windows, which uvicorn
doesn't guarantee.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import shutil
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import IO

from .errors import ServiceError

log = logging.getLogger(__name__)

PROTOCOL_VERSION = 1

# JSON-RPC's own error codes.
METHOD_NOT_FOUND = -32601
INTERNAL_ERROR = -32603

# How often a blocked request re-checks its cancel token and deadline.
_POLL_SECONDS = 0.1
# Enough stderr to explain a crash in an error message, not a whole log.
_STDERR_TAIL_LINES = 20
# 0 off Windows, where creationflags must be 0.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

RequestHandler = Callable[[str, dict], object]
NotificationHandler = Callable[[str, dict], None]


class AcpError(ServiceError):
    """The agent couldn't be started, died, timed out, or answered with an error."""


class AcpCancelled(AcpError):
    """The caller's cancel token was set while waiting on the agent."""


class AcpRemoteError(AcpError):
    """Raise from a request handler to answer the agent with a JSON-RPC error."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def resolve_command(command: list[str] | None) -> list[str] | None:
    """Resolves a command's executable on PATH, or returns None if it isn't
    installed.

    This matters on Windows: npm installs `cline` as `cline.cmd`, which
    CreateProcess only finds by its full path."""
    if not command:
        return None
    found = shutil.which(command[0])
    return [found, *command[1:]] if found else None


class AcpConnection:
    """One agent process. Use as a context manager so the process is always
    reaped, including on cancellation.

    on_request answers the agent's own requests (session/request_permission,
    fs/*): return the JSON result, or raise AcpRemoteError. on_notification
    receives session/update and anything else sent without an id. Both run on
    the reader thread, so they must not block on this connection."""

    def __init__(
        self,
        command: list[str],
        cwd: str,
        on_request: RequestHandler,
        on_notification: NotificationHandler,
        env: dict[str, str] | None = None,
    ) -> None:
        resolved = resolve_command(command)
        if resolved is None:
            raise AcpError(f"{command[0] if command else '(empty command)'} was not found on PATH")
        self._on_request = on_request
        self._on_notification = on_notification
        self._next_id = 0
        self._pending: dict[int, queue.Queue] = {}
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._stderr_tail: deque[str] = deque(maxlen=_STDERR_TAIL_LINES)
        try:
            self._proc = subprocess.Popen(
                resolved,
                cwd=cwd,
                env=env if env is not None else os.environ.copy(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                # Windows: `cline` is an npm .cmd shim, and a console program
                # started from a console-less server gets a visible window of
                # its own. A windowless console is also inherited by anything
                # the agent spawns (npx, git), so none of those pop up either.
                creationflags=_NO_WINDOW,
            )
        except OSError as exc:
            raise AcpError(f"could not start {resolved[0]}: {exc}") from exc
        # Popen types these Optional because they are None unless that stream
        # was piped. All three are PIPE above, so they never are here - bound
        # once so the rest of the class can use them without re-establishing
        # that at every call site.
        self._stdin: IO[bytes] = self._proc.stdin  # type: ignore[assignment]
        self._stdout: IO[bytes] = self._proc.stdout  # type: ignore[assignment]
        self._stderr: IO[bytes] = self._proc.stderr  # type: ignore[assignment]
        self._stdout_thread = threading.Thread(target=self._read_stdout, name="acp-stdout", daemon=True)
        self._stdout_thread.start()
        threading.Thread(target=self._read_stderr, name="acp-stderr", daemon=True).start()

    def __enter__(self) -> AcpConnection:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def request(self, method: str, params: dict, timeout: float, cancel: threading.Event | None = None) -> dict:
        """Sends a request and blocks until its response, the deadline, the
        cancel token, or the agent exiting — whichever comes first."""
        with self._lock:
            self._next_id += 1
            msg_id = self._next_id
            waiter: queue.Queue = queue.Queue(maxsize=1)
            self._pending[msg_id] = waiter
        self._send({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        try:
            while True:
                try:
                    message = waiter.get(timeout=_POLL_SECONDS)
                    break
                except queue.Empty:
                    pass
                if cancel is not None and cancel.is_set():
                    raise AcpCancelled(f"{method} cancelled")
                if time.monotonic() > deadline:
                    raise AcpError(f"agent did not answer {method} within {timeout:.0f}s")
                if self._proc.poll() is not None:
                    # Its last response may still be in the pipe: let the
                    # reader drain it before deciding the answer never came.
                    self._stdout_thread.join(timeout=1)
                    if waiter.empty():
                        raise AcpError(
                            f"agent exited (code {self._proc.returncode}) during {method}{self._stderr_hint()}"
                        )
        finally:
            with self._lock:
                self._pending.pop(msg_id, None)
        if "error" in message:
            error = message["error"] or {}
            detail = error.get("message", error)
            # Agents put the real cause in `data` ("Internal error" alone
            # names nothing), so it's worth the length.
            if error.get("data"):
                detail = f"{detail} ({json.dumps(error['data'])[:500]})"
            raise AcpError(f"agent rejected {method}: {detail}")
        return message.get("result") or {}

    def notify(self, method: str, params: dict) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def close(self) -> None:
        """Closes stdin (the polite ask to exit), then kills the whole process
        tree if it lingers. A tree kill because on Windows `cline.cmd` runs
        node under cmd.exe, and terminating cmd.exe alone orphans node."""
        if self._proc.poll() is None:
            try:
                self._stdin.close()
            except OSError:
                pass
            try:
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                _kill_tree(self._proc)

    def _send(self, message: dict) -> None:
        data = (json.dumps(message) + "\n").encode("utf-8")
        with self._write_lock:
            try:
                self._stdin.write(data)
                self._stdin.flush()
            except (OSError, ValueError) as exc:
                raise AcpError(f"agent is not accepting input: {exc}{self._stderr_hint()}") from exc

    def _read_stdout(self) -> None:
        for raw in self._stdout:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                # stdout is reserved for protocol messages; anything else is
                # an agent bug worth a log line, not a dead connection.
                log.info("acp: ignoring non-JSON stdout line: %.200s", line)
                continue
            if isinstance(message, dict):
                self._dispatch(message)

    def _dispatch(self, message: dict) -> None:
        method = message.get("method")
        msg_id = message.get("id")
        if method is None:
            with self._lock:
                waiter = self._pending.get(msg_id) if isinstance(msg_id, int) else None
            if waiter is not None:
                waiter.put(message)
            return
        params = message.get("params") or {}
        if msg_id is None:
            try:
                self._on_notification(method, params)
            except Exception:  # a bad handler must not kill the reader thread
                log.exception("acp: notification handler failed for %s", method)
            return
        try:
            response = {"jsonrpc": "2.0", "id": msg_id, "result": self._on_request(method, params)}
        except AcpRemoteError as exc:
            response = {"jsonrpc": "2.0", "id": msg_id, "error": {"code": exc.code, "message": str(exc)}}
        except Exception as exc:
            log.exception("acp: request handler failed for %s", method)
            response = {"jsonrpc": "2.0", "id": msg_id, "error": {"code": INTERNAL_ERROR, "message": str(exc)}}
        try:
            self._send(response)
        except AcpError:
            pass  # the agent is gone; request() reports that to the caller

    def _read_stderr(self) -> None:
        for raw in self._stderr:
            self._stderr_tail.append(raw.decode("utf-8", errors="replace").rstrip())

    def _stderr_hint(self) -> str:
        tail = [line for line in self._stderr_tail if line]
        return f" — agent stderr: {' | '.join(tail[-3:])}" if tail else ""


def _kill_tree(proc: subprocess.Popen) -> None:
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, check=False, creationflags=_NO_WINDOW
        )
    else:
        proc.kill()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        log.warning("acp: agent process %d did not exit after kill", proc.pid)
