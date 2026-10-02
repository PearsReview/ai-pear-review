import { existsSync } from "node:fs";
import * as path from "node:path";
import * as vscode from "vscode";

// Which interpreter runs the backend. The setting wins; then the extension's own
// .venv (the dev setup — bundled libs replace it at packaging); then PATH.
// The ms-python extension's interpreter API is the planned next step before PATH.
export function resolvePython(extensionPath: string): string {
  const configured = vscode.workspace.getConfiguration("pearReview").get<string>("pythonPath")?.trim();
  if (configured) return configured;
  const venv =
    process.platform === "win32"
      ? path.join(extensionPath, ".venv", "Scripts", "python.exe")
      : path.join(extensionPath, ".venv", "bin", "python");
  return existsSync(venv) ? venv : "python";
}
