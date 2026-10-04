import { existsSync } from "node:fs";
import * as path from "node:path";
import * as vscode from "vscode";

import { SETUP_PYTHON } from "../log.ts";

// Which interpreter runs the backend and the audio player, in order:
// 1. the pearReview.pythonPath setting;
// 2. the environment "Set Up Python Environment" made, in the extension's global storage;
// 3. the extension's own .venv (the development setup, README "Developing").
// Nothing else: a Python found on PATH rarely has the backend's packages, and failing
// on a missing import is worse than saying how to set one up.

let storageDir: string | undefined;

// Called once from activate(): where the set-up environment lives.
export function setStorageDir(dir: string): void {
  storageDir = dir;
}

// The environment Set Up Python Environment creates and fills.
export function managedVenv(): string {
  if (!storageDir) throw new Error("The extension's storage isn't known yet.");
  return path.join(storageDir, "venv");
}

export function venvPython(venv: string): string {
  return process.platform === "win32" ? path.join(venv, "Scripts", "python.exe") : path.join(venv, "bin", "python");
}

// No interpreter is set up: the message says how, and showError offers the command.
export class PythonSetupNeeded extends Error {
  constructor() {
    super(`No Python environment is set up for Pear Review. Run "${SETUP_PYTHON}", or set pearReview.pythonPath.`);
  }
}

export function resolvePython(extensionPath: string): string {
  const configured = vscode.workspace.getConfiguration("pearReview").get<string>("pythonPath")?.trim();
  if (configured) return configured;
  for (const venv of [storageDir ? managedVenv() : undefined, path.join(extensionPath, ".venv")]) {
    if (venv && existsSync(venvPython(venv))) return venvPython(venv);
  }
  throw new PythonSetupNeeded();
}
