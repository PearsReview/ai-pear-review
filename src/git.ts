import * as path from "node:path";
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

// Which reviewed file, and which side of its diff, an editor document is. The git
// extension's HEAD side (toGitUri) keeps the file's own path; anything outside the
// repo, or any other scheme, is not part of the review.
export function reviewLocation(
  uri: vscode.Uri,
  repoRoot: string,
): { filePath: string; side: "new" | "old" } | undefined {
  const side = uri.scheme === "file" ? "new" : uri.scheme === "git" ? "old" : undefined;
  if (!side) return undefined;
  const relative = path.relative(repoRoot, uri.fsPath);
  if (!relative || relative.startsWith("..") || path.isAbsolute(relative)) return undefined;
  return { filePath: relative.split(path.sep).join("/"), side };
}

// The reverse: the document a file's side opens as in the review diff.
export async function reviewUri(repoRoot: string, filePath: string, side: "new" | "old"): Promise<vscode.Uri> {
  const fileUri = vscode.Uri.file(path.join(repoRoot, filePath));
  if (side === "new") return fileUri;
  const git = await gitApi();
  return git ? git.toGitUri(fileUri, "HEAD") : fileUri;
}
