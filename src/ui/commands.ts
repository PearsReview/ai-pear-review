import * as path from "node:path";
import * as vscode from "vscode";

import type { Backend } from "../backend/backend.ts";
import { pickRepository, repositoryRoots } from "../git.ts";
import { output, showError } from "../log.ts";
import type { Prefs } from "./prefs.ts";
import type { Voice } from "./voice.ts";

export function register(
  context: vscode.ExtensionContext,
  backend: Backend,
  voice: Voice,
  prefs: Prefs,
): vscode.Disposable[] {
  let currentIndex: number | undefined;

  const send = (fn: () => void): void => {
    try {
      fn();
    } catch (err) {
      showError(err instanceof Error ? err.message : String(err));
    }
  };

  // Opens the changes without starting the review: the tree, the diffs and the chat all
  // work before Start Review, which only turns on narration, reviewed marks and comments.
  // `quiet` is the automatic open when the view first shows: no prompts, and nothing at
  // all unless the workspace has exactly one repository.
  const openChanges = async (options?: unknown): Promise<boolean> => {
    const quiet = typeof options === "object" && options !== null && (options as { quiet?: unknown }).quiet === true;
    if (backend.state === "ready") return true;
    if (backend.state === "starting") return untilReady();
    if (vscode.env.remoteName) {
      if (!quiet) showError("Remote workspaces aren't supported. Open the repository locally to review it.");
      return false;
    }
    let repo: string | undefined;
    if (quiet) {
      const roots = await repositoryRoots();
      if (roots.length !== 1) return false;
      repo = roots[0];
    } else {
      repo = await pickRepository();
    }
    if (!repo) return false;
    try {
      await vscode.window.withProgress(
        { location: vscode.ProgressLocation.Window, title: "Pear Review: starting backend" },
        () => backend.start(repo),
      );
      return true;
    } catch (err) {
      showError(err instanceof Error ? err.message : String(err));
      return false;
    }
  };

  const untilReady = (): Promise<boolean> =>
    new Promise((resolve) => {
      const subscription = backend.onStateChange((state) => {
        if (state === "starting") return;
        subscription.dispose();
        resolve(state === "ready");
      });
    });

  const startReview = async (): Promise<void> => {
    if (!(await openChanges())) return;
    await vscode.commands.executeCommand("pearReview.chat.focus");
    send(() => backend.send("start_review", {}));
  };

  return [
    backend.on("presenting", (p) => (currentIndex = p.done ? undefined : p.index)),
    vscode.commands.registerCommand("pearReview.openChanges", openChanges),
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
    vscode.commands.registerCommand("pearReview.reviewAll", () => send(() => backend.send("toggle_reviewed_all", {}))),
    vscode.commands.registerCommand("pearReview.showSummary", () => send(() => backend.send("show_summary", {}))),
    // `{ confirmed: true }` skips the question, for a keybinding or the tests.
    vscode.commands.registerCommand("pearReview.newReview", async (preset?: unknown) => {
      const confirmed =
        typeof preset === "object" && preset !== null && (preset as { confirmed?: unknown }).confirmed === true;
      const choice =
        confirmed ||
        (await vscode.window.showWarningMessage(
          "Start a new review? Reviewed marks are cleared and the changes are read again. Plans already written stay in .review/.",
          { modal: true },
          "Start New Review",
        ));
      if (!choice) return;
      send(() => {
        backend.send("new_review", {});
        backend.send("start_review", {});
      });
    }),
    vscode.commands.registerCommand("pearReview.openPlan", (file: unknown) => {
      if (typeof file === "string" && backend.repoPath) {
        const uri = vscode.Uri.file(path.join(backend.repoPath, file));
        void vscode.commands.executeCommand("markdown.showPreview", uri);
      }
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
    vscode.commands.registerCommand("pearReview.toggleRecording", () => {
      if (!prefs.values.stt && !voice.recording) {
        void vscode.window.showInformationMessage("Voice input is off. Turn it on in the chat's settings (⚙).");
        return;
      }
      voice.toggle();
    }),
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
