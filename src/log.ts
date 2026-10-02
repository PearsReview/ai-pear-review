import * as vscode from "vscode";

// One output channel for the extension and the backend's own stdout/stderr, so a
// failure report is one "Show Log" away.
export const output = vscode.window.createOutputChannel("Pear Review");

export function log(line: string): void {
  output.appendLine(line);
}

// Every user-facing failure goes through here: logged, then shown with a way to the log.
export function showError(message: string): void {
  log(`ERROR ${message}`);
  void vscode.window.showErrorMessage(`Pear Review: ${message}`, "Show Log").then((choice) => {
    if (choice) output.show();
  });
}
