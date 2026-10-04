import * as vscode from "vscode";

// One output channel for the extension and the backend's own stdout/stderr, so a
// failure report is one "Show Log" away.
export const output = vscode.window.createOutputChannel("Pear Review");

// The command a missing or incomplete Python environment is fixed with. An error that
// names it gets a button that runs it.
export const SETUP_PYTHON = "Pear Review: Set Up Python Environment";

export function log(line: string): void {
  output.appendLine(line);
}

// Every user-facing failure goes through here: logged, then shown with a way to the log
// (and to the Python set-up when that's the fix).
export function showError(message: string): void {
  log(`ERROR ${message}`);
  const setUp = message.includes(SETUP_PYTHON) ? ["Set Up Python"] : [];
  void vscode.window.showErrorMessage(`Pear Review: ${message}`, ...setUp, "Show Log").then((choice) => {
    if (choice === "Show Log") output.show();
    else if (choice === "Set Up Python") void vscode.commands.executeCommand("pearReview.setupPython");
  });
}
