import * as path from "node:path";
import * as vscode from "vscode";

import type { Backend } from "../backend/backend.ts";
import { repoContaining, repositoryRoots, reviewLocation } from "../git.ts";
import { log, showError } from "../log.ts";
import { previewFile, previewTab } from "./previewTabs.ts";
import type { ChatTarget } from "./target.ts";

// Files beyond the changes under review: which file a command means, the backend for
// its repository, and asking the reviewer about any file (explore_reply). Read Aloud
// (readAloud.ts) finds its file the same way.

// The file a command was invoked on. The Explorer and editor menus pass its URI; a
// markdown preview passes nothing, so its file is worked out from the tab
// (previewTabs.ts); otherwise the active editor's.
export async function targetUri(arg: unknown): Promise<vscode.Uri | undefined> {
  if (arg instanceof vscode.Uri && arg.scheme === "file") return arg;
  const tab = previewTab();
  if (tab) {
    const file = await previewFile(tab);
    if (file) return file;
    log(`Can't tell which file the preview "${tab.label}" shows.`);
    throw new Error(
      `Couldn't tell which file "${tab.label}" shows (more than one has that name). Open the file's text and use the command there.`,
    );
  }
  return vscode.window.activeTextEditor?.document.uri;
}

// The file's repository-relative path, with that repository shown (its backend started
// if need be, without starting the review). Undefined once it has said why not.
export async function ensureBackendFor(backend: Backend, uri: vscode.Uri): Promise<string | undefined> {
  if (vscode.env.remoteName) {
    showError("Remote workspaces aren't supported. Open the repository locally.");
    return undefined;
  }
  const root = repoContaining(uri, await repositoryRoots());
  if (!root) {
    showError("That file isn't in a git repository VS Code has open.");
    return undefined;
  }
  // The file's repository becomes the one shown (each keeps its own backend).
  const shown = backend.repoPath && path.relative(backend.repoPath, root) === "";
  if (backend.state !== "ready" || !shown) {
    await vscode.window.withProgress(
      { location: vscode.ProgressLocation.Window, title: "Pear Review: starting backend" },
      () => backend.start(root),
    );
  }
  return reviewLocation(uri, root)?.filePath;
}

export function register(backend: Backend, target: ChatTarget): vscode.Disposable[] {
  const askAboutFile = async (arg: unknown): Promise<void> => {
    const uri = await targetUri(arg);
    if (!uri || uri.scheme !== "file") {
      showError("Open or select a file in the repository first.");
      return;
    }
    const filePath = await ensureBackendFor(backend, uri);
    if (!filePath) return;
    if (vscode.window.activeTextEditor?.document.uri.toString() !== uri.toString()) {
      await vscode.window.showTextDocument(uri, { preview: true });
    }
    target.setFile(filePath);
    await vscode.commands.executeCommand("pearReview.chat.focus");
  };

  return [
    vscode.commands.registerCommand("pearReview.askAboutFile", (arg: unknown) =>
      askAboutFile(arg).catch((err: unknown) => showError(err)),
    ),
    vscode.commands.registerCommand("pearReview.openRepoFile", (filePath: unknown) => {
      if (typeof filePath === "string" && backend.repoPath) {
        void vscode.window.showTextDocument(vscode.Uri.file(path.join(backend.repoPath, filePath)));
      }
    }),
  ];
}
