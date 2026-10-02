import * as vscode from "vscode";

import type { Backend } from "../backend/backend.ts";
import { pickRepository } from "../git.ts";
import { output, showError } from "../log.ts";
import type { Voice } from "./voice.ts";

export function register(context: vscode.ExtensionContext, backend: Backend, voice: Voice): vscode.Disposable[] {
  let currentIndex: number | undefined;

  const send = (fn: () => void): void => {
    try {
      fn();
    } catch (err) {
      showError(err instanceof Error ? err.message : String(err));
    }
  };

  const startReview = async (): Promise<void> => {
    if (vscode.env.remoteName) {
      showError("Remote workspaces aren't supported. Open the repository locally to review it.");
      return;
    }
    const repo = await pickRepository();
    if (!repo) return;
    try {
      await vscode.window.withProgress(
        { location: vscode.ProgressLocation.Window, title: "Pear Review: starting backend" },
        () => backend.start(repo),
      );
    } catch (err) {
      showError(err instanceof Error ? err.message : String(err));
      return;
    }
    await vscode.commands.executeCommand("pearReview.chat.focus");
    send(() => backend.send("start_review", {}));
  };

  return [
    backend.on("presenting", (p) => (currentIndex = p.done ? undefined : p.index)),
    vscode.commands.registerCommand("pearReview.startReview", startReview),
    vscode.commands.registerCommand("pearReview.stopBackend", () => backend.stop()),
    vscode.commands.registerCommand("pearReview.next", () => send(() => backend.send("next", {}))),
    vscode.commands.registerCommand("pearReview.prev", () => send(() => backend.send("prev", {}))),
    vscode.commands.registerCommand("pearReview.jumpToHunk", (index: unknown) => {
      if (typeof index === "number") send(() => backend.send("jump_to_hunk", { index }));
    }),
    vscode.commands.registerCommand("pearReview.toggleReviewed", () => send(() => backend.send("toggle_reviewed", {}))),
    vscode.commands.registerCommand("pearReview.endReview", async () => {
      const choice = await vscode.window.showWarningMessage(
        "End the review? Explanations, replies and reviewed marks stop; queued comments stay for Create Plan.",
        { modal: true },
        "End Review",
      );
      if (choice) send(() => backend.send("end_review", {}));
    }),
    vscode.commands.registerCommand("pearReview.interrupt", () => send(() => backend.send("stop", {}))),
    vscode.commands.registerCommand("pearReview.refresh", () => send(() => backend.send("refresh_diff", {}))),
    vscode.commands.registerCommand("pearReview.explain", () => {
      if (currentIndex === undefined) {
        showError("No change is on screen to explain.");
        return;
      }
      const index = currentIndex;
      send(() => backend.send("explain_hunk", { index }));
    }),
    vscode.commands.registerCommand("pearReview.toggleRecording", () => voice.toggle()),
    vscode.commands.registerCommand("pearReview.showLog", () => output.show()),
    vscode.commands.registerCommand("pearReview.setAnthropicApiKey", async () => {
      const key = await vscode.window.showInputBox({
        prompt: "Anthropic API key (stored in VS Code's secret storage; used on the next backend start)",
        password: true,
        ignoreFocusOut: true,
      });
      if (key === undefined) return;
      if (key.trim()) await context.secrets.store("pearReview.anthropicApiKey", key.trim());
      else await context.secrets.delete("pearReview.anthropicApiKey");
    }),
  ];
}
