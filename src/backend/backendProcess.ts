import { spawn, type ChildProcess } from "node:child_process";
import * as path from "node:path";

import { log } from "../log.ts";

const PORT_LINE = /^PEAR_REVIEW_PORT=(\d+)\s*$/m;
// Startup includes the preflight, which pings Ollama / the voice service. Generous,
// because a cold model server can take a while to answer its first request.
const READY_TIMEOUT_MS = 60_000;
const POLL_INTERVAL_MS = 250;
// How long a stopped backend gets to exit before it's killed outright.
const KILL_GRACE_MS = 3_000;

export interface StartOptions {
  python: string;
  extensionPath: string;
  repoPath: string;
  env?: Record<string, string>;
}

// The Python child process: spawn python/launch.py, read the port it chose, wait
// until the server answers HTTP, and stop it again. Knows nothing about the protocol.
export class BackendProcess {
  private child: ChildProcess | undefined;
  private exitListeners: ((code: number | null) => void)[] = [];

  onExit(listener: (code: number | null) => void): void {
    this.exitListeners.push(listener);
  }

  async start(options: StartOptions): Promise<number> {
    const launcher = path.join(options.extensionPath, "python", "launch.py");
    log(`Starting backend: ${options.python} ${launcher} --repo ${options.repoPath}`);
    const child = spawn(options.python, ["-u", launcher, "--repo", options.repoPath], {
      cwd: options.repoPath,
      env: { ...process.env, ...options.env, REVIEW_NO_BROWSER: "1", PYTHONIOENCODING: "utf-8" },
      windowsHide: true,
    });
    this.child = child;

    const port = await new Promise<number>((resolve, reject) => {
      let buffered = "";
      const onData = (chunk: Buffer): void => {
        const text = chunk.toString("utf8");
        buffered += text;
        const match = PORT_LINE.exec(buffered);
        if (match?.[1]) resolve(Number(match[1]));
      };
      child.stdout?.on("data", onData);
      child.stdout?.on("data", (chunk: Buffer) => log(chunk.toString("utf8").trimEnd()));
      child.stderr?.on("data", (chunk: Buffer) => log(chunk.toString("utf8").trimEnd()));
      child.once("error", (err) => reject(new Error(`Could not start ${options.python}: ${err.message}`)));
      child.once("exit", (code) => {
        reject(new Error(`The backend exited during startup (code ${code}). See the log for the reason.`));
        this.child = undefined;
        for (const listener of this.exitListeners) listener(code);
      });
    });

    await waitUntilServing(port);
    return port;
  }

  async stop(): Promise<void> {
    const child = this.child;
    if (!child || child.exitCode !== null) return;
    this.child = undefined;
    const exited = new Promise<void>((resolve) => child.once("exit", () => resolve()));
    child.kill();
    const timer = setTimeout(() => child.kill("SIGKILL"), KILL_GRACE_MS);
    await exited;
    clearTimeout(timer);
  }
}

async function waitUntilServing(port: number): Promise<void> {
  const deadline = Date.now() + READY_TIMEOUT_MS;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(`http://127.0.0.1:${port}/`);
      if (response.ok) return;
    } catch {
      // Not listening yet.
    }
    await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS));
  }
  throw new Error(`The backend didn't answer on port ${port} within ${READY_TIMEOUT_MS / 1000}s.`);
}
