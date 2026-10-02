import * as vscode from "vscode";

import type { Backend } from "../backend/backend.ts";
import { output, showError } from "../log.ts";
import type { Voice } from "./voice.ts";

// The slice of the built-in vscode.git extension's API this uses.
interface GitApi {
  repositories: { rootUri: vscode.Uri }[];
}

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

// The workspace's git repository; a quick pick when there are several.
async function pickRepository(): Promise<string | undefined> {
  const gitExtension = vscode.extensions.getExtension<{ getAPI(version: 1): GitApi }>("vscode.git");
  const git = gitExtension ? (await gitExtension.activate()).getAPI(1) : undefined;
  const roots = git?.repositories.map((r) => r.rootUri.fsPath) ?? [];
  if (roots.length === 0) {
    showError("Open a git repository to start a review.");
    return undefined;
  }
  if (roots.length === 1) return roots[0];
  return vscode.window.showQuickPick(roots, { placeHolder: "Which repository do you want to review?" });
}
