import { execFile, spawn } from "node:child_process";
import { existsSync, rmSync } from "node:fs";
import * as path from "node:path";
import * as vscode from "vscode";

import { managedVenv, venvPython } from "../backend/python.ts";
import { errorMessage, log, output, showError } from "../log.ts";

// "Set Up Python Environment": a venv in the extension's global storage with the
// backend's packages and sounddevice (the mic and the audio player), made from a
// Python 3.10+ found on this machine. One platform-neutral .vsix, at the cost of a
// download on first use; resolvePython (backend/python.ts) uses it from then on.

const MIN_VERSION = [3, 10] as const;

interface Candidate {
  command: string;
  args: string[];
  label: string;
}

// The interpreters to make the venv from, best first: the one the Python extension has
// selected for this workspace, then the launcher and the usual names on PATH, then the
// versioned python3.x names (newest first) a Homebrew or pyenv install goes by.
async function candidates(): Promise<Candidate[]> {
  const found: Candidate[] = [];
  const configured = vscode.workspace.getConfiguration("pearReview").get<string>("pythonPath")?.trim();
  if (configured) found.push({ command: configured, args: [], label: `${configured} (pearReview.pythonPath)` });
  const pythonExt = vscode.extensions.getExtension<PythonExtensionApi>("ms-python.python");
  if (pythonExt) {
    try {
      const api = await pythonExt.activate();
      const active = api.environments.getActiveEnvironmentPath();
      const resolved = await api.environments.resolveEnvironment(active);
      const executable = resolved?.executable.uri?.fsPath;
      if (executable) found.push({ command: executable, args: [], label: `${executable} (Python extension)` });
    } catch (err) {
      log(`Python setup: the Python extension didn't give an interpreter (${String(err)}).`);
    }
  }
  if (process.platform === "win32") found.push({ command: "py", args: ["-3"], label: "py -3" });
  found.push({ command: "python3", args: [], label: "python3" }, { command: "python", args: [], label: "python" });
  // Versioned names, newest first: on macOS `python3` is often Apple's old build (or
  // absent), while a usable 3.1x from Homebrew or pyenv is only on PATH as python3.12 etc.
  for (let minor = 13; minor >= MIN_VERSION[1]; minor--) {
    found.push({ command: `python${MIN_VERSION[0]}.${minor}`, args: [], label: `python${MIN_VERSION[0]}.${minor}` });
  }
  return found;
}

// The slice of the ms-python.python extension's API used here.
interface PythonExtensionApi {
  environments: {
    getActiveEnvironmentPath(): { path: string };
    resolveEnvironment(env: { path: string }): Promise<{ executable: { uri?: vscode.Uri } } | undefined>;
  };
}

// The candidate's version and real executable, or undefined when it isn't there or
// is older than 3.10.
function probe(candidate: Candidate): Promise<string | undefined> {
  const script = "import sys; print('%d.%d' % sys.version_info[:2]); print(sys.executable)";
  return new Promise((resolve) => {
    execFile(candidate.command, [...candidate.args, "-c", script], { windowsHide: true }, (err, stdout) => {
      if (err) return resolve(undefined);
      const [version, executable] = stdout.trim().split(/\r?\n/);
      const [major = 0, minor = 0] = (version ?? "").split(".").map(Number);
      const recent = major > MIN_VERSION[0] || (major === MIN_VERSION[0] && minor >= MIN_VERSION[1]);
      log(`Python setup: ${candidate.label} is Python ${version ?? "?"}${recent ? "" : " (too old)"}.`);
      resolve(recent ? executable : undefined);
    });
  });
}

// Runs a command, its output going to the log; rejects on a non-zero exit.
function run(command: string, args: string[], token: vscode.CancellationToken): Promise<void> {
  log(`Python setup: ${command} ${args.join(" ")}`);
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { windowsHide: true });
    const cancel = token.onCancellationRequested(() => child.kill());
    child.stdout.on("data", (chunk: Buffer) => output.append(chunk.toString("utf8")));
    child.stderr.on("data", (chunk: Buffer) => output.append(chunk.toString("utf8")));
    child.once("error", (err) => {
      cancel.dispose();
      reject(err);
    });
    child.once("exit", (code) => {
      cancel.dispose();
      if (token.isCancellationRequested) reject(new vscode.CancellationError());
      else if (code === 0) resolve();
      else reject(new Error(`${path.basename(command)} exited with code ${code}. See the log.`));
    });
  });
}

export function register(context: vscode.ExtensionContext): vscode.Disposable[] {
  const setUp = async (): Promise<void> => {
    let base: string | undefined;
    for (const candidate of await candidates()) {
      base = await probe(candidate);
      if (base) break;
    }
    if (!base) {
      const choice = await vscode.window.showErrorMessage(
        `Pear Review needs Python ${MIN_VERSION.join(".")} or later, and none was found. Install it, then run this again.`,
        "Get Python",
      );
      if (choice) void vscode.env.openExternal(vscode.Uri.parse("https://www.python.org/downloads/"));
      return;
    }
    const venv = managedVenv();
    // The .vsix holds the backend in backend/; a checkout (F5) has it one level up.
    const packaged = path.join(context.extensionPath, "backend", "requirements.txt");
    const requirements = existsSync(packaged) ? packaged : path.join(context.extensionPath, "..", "requirements.txt");
    try {
      await vscode.window.withProgress(
        { location: vscode.ProgressLocation.Notification, title: "Pear Review: setting up Python", cancellable: true },
        async (progress, token) => {
          output.show(true);
          progress.report({ message: `creating an environment from ${base}` });
          rmSync(venv, { recursive: true, force: true });
          await run(base, ["-m", "venv", venv], token);
          progress.report({ message: "installing the backend's packages (a few minutes the first time)" });
          await run(
            venvPython(venv),
            ["-m", "pip", "install", "--disable-pip-version-check", "-r", requirements, "sounddevice>=0.4,<1.0"],
            token,
          );
        },
      );
    } catch (err) {
      if (err instanceof vscode.CancellationError) return;
      showError(`Setting up Python failed: ${errorMessage(err)}`);
      return;
    }
    log(`Python setup: ready at ${venv}.`);
    void vscode.window.showInformationMessage("Pear Review: Python is set up. Open the Pear Review view to start.");
  };

  return [
    vscode.commands.registerCommand("pearReview.setupPython", () => setUp().catch((err: unknown) => showError(err))),
  ];
}
