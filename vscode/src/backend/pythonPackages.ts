import { spawn } from "node:child_process";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import * as path from "node:path";
import * as vscode from "vscode";

import { SETUP_PYTHON, errorMessage, log, output } from "../log.ts";
import { managedVenv, venvPython } from "./python.ts";
import { EXTRA_PACKAGES, STAMP_FILE, requirementsStamp } from "./requirements.ts";

// Keeps the environment Set Up Python Environment made in step with the backend it
// runs. Setup installs requirements.txt once; a release that adds a package (openai,
// for the OpenAI-compatible provider) would otherwise leave it missing until the
// reviewer ran the command again. So the backend's start checks a stamp of the file
// it was filled from, and reinstalls when the file has changed.

// The .vsix holds the backend in backend/; a checkout (F5) has it one level up.
export function requirementsPath(extensionPath: string): string {
  const packaged = path.join(extensionPath, "backend", "requirements.txt");
  return existsSync(packaged) ? packaged : path.join(extensionPath, "..", "requirements.txt");
}

// Runs a command, its output going to the log; rejects on a non-zero exit.
export function run(command: string, args: string[], token?: vscode.CancellationToken): Promise<void> {
  log(`Python setup: ${command} ${args.join(" ")}`);
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { windowsHide: true });
    const cancel = token?.onCancellationRequested(() => child.kill());
    child.stdout.on("data", (chunk: Buffer) => output.append(chunk.toString("utf8")));
    child.stderr.on("data", (chunk: Buffer) => output.append(chunk.toString("utf8")));
    child.once("error", (err) => {
      cancel?.dispose();
      reject(err);
    });
    child.once("exit", (code) => {
      cancel?.dispose();
      if (token?.isCancellationRequested) reject(new vscode.CancellationError());
      else if (code === 0) resolve();
      else reject(new Error(`${path.basename(command)} exited with code ${code}. See the log.`));
    });
  });
}

// Installs the backend's packages into a venv and records what they came from.
export async function installPackages(
  venv: string,
  extensionPath: string,
  token?: vscode.CancellationToken,
): Promise<void> {
  const requirements = requirementsPath(extensionPath);
  await run(
    venvPython(venv),
    ["-m", "pip", "install", "--disable-pip-version-check", "-r", requirements, ...EXTRA_PACKAGES],
    token,
  );
  writeFileSync(path.join(venv, STAMP_FILE), requirementsStamp(readFileSync(requirements, "utf8")), "utf8");
}

// Before the backend starts: brings the managed venv up to this release's
// requirements, if it has one and it's out of date. A venv from before stamps existed
// counts as out of date once (pip skips what's already there). Never throws: a failed
// install still starts the backend, which says what's missing, as it did before.
export async function refreshPythonPackages(extensionPath: string): Promise<void> {
  let venv: string;
  try {
    venv = managedVenv();
  } catch {
    return;
  }
  if (!existsSync(venvPython(venv))) return; // not set up: resolvePython says how
  let current: string;
  let saved: string | undefined;
  try {
    current = requirementsStamp(readFileSync(requirementsPath(extensionPath), "utf8"));
    const stamp = path.join(venv, STAMP_FILE);
    saved = existsSync(stamp) ? readFileSync(stamp, "utf8").trim() : undefined;
  } catch (err) {
    log(`Python packages: couldn't compare with requirements.txt (${errorMessage(err)}).`);
    return;
  }
  if (saved === current) return;
  log("Python packages: requirements.txt changed since this environment was filled; updating it.");
  try {
    await vscode.window.withProgress(
      { location: vscode.ProgressLocation.Notification, title: "Pear Review: updating Python packages" },
      () => installPackages(venv, extensionPath),
    );
    log("Python packages: up to date.");
  } catch (err) {
    log(`Python packages: update failed (${errorMessage(err)}).`);
    void vscode.window.showWarningMessage(
      `Pear Review couldn't update its Python packages (${errorMessage(err)}). If something doesn't work, run "${SETUP_PYTHON}".`,
    );
  }
}
