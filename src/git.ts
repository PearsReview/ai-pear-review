import * as vscode from "vscode";

import { showError } from "./log.ts";

// The slice of the built-in vscode.git extension's API this extension uses.
interface GitApi {
  repositories: { rootUri: vscode.Uri }[];
  toGitUri(uri: vscode.Uri, ref: string): vscode.Uri;
}

let api: GitApi | undefined;

export async function gitApi(): Promise<GitApi | undefined> {
  if (api) return api;
  const extension = vscode.extensions.getExtension<{ getAPI(version: 1): GitApi }>("vscode.git");
  api = extension ? (await extension.activate()).getAPI(1) : undefined;
  return api;
}

// The workspace's git repository; a quick pick when there are several.
export async function pickRepository(): Promise<string | undefined> {
  const roots = (await gitApi())?.repositories.map((r) => r.rootUri.fsPath) ?? [];
  if (roots.length === 0) {
    showError("Open a git repository to start a review.");
    return undefined;
  }
  if (roots.length === 1) return roots[0];
  return vscode.window.showQuickPick(roots, { placeHolder: "Which repository do you want to review?" });
}
