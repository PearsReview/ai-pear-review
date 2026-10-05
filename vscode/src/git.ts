import * as path from "node:path";
import * as vscode from "vscode";

import { prReviewFor } from "./github/prReviews.ts";
import { showError } from "./log.ts";

// The slice of the built-in vscode.git extension's API this extension uses.
interface GitApi {
  state: "uninitialized" | "initialized";
  onDidChangeState: vscode.Event<"uninitialized" | "initialized">;
  repositories: { rootUri: vscode.Uri }[];
  onDidOpenRepository: vscode.Event<{ rootUri: vscode.Uri }>;
  onDidCloseRepository: vscode.Event<{ rootUri: vscode.Uri }>;
  toGitUri(uri: vscode.Uri, ref: string): vscode.Uri;
  // A pull request review's worktree isn't in the workspace, so it's opened by hand.
  openRepository(root: vscode.Uri): Promise<unknown>;
}

let api: GitApi | undefined;

// Right after VS Code opens, the git extension is still finding repositories: Start
// Review pressed then would see none. Caught by the integration suite's first test.
const DISCOVERY_WAIT_MS = 5_000;

export async function gitApi(): Promise<GitApi | undefined> {
  if (api) return api;
  const extension = vscode.extensions.getExtension<{ getAPI(version: 1): GitApi }>("vscode.git");
  api = extension ? (await extension.activate()).getAPI(1) : undefined;
  return api;
}

// The git extension's repositories, once it has finished starting and has had a moment
// to open the workspace's (it opens them one by one after initializing).
export async function repositoryRoots(): Promise<string[]> {
  const git = await gitApi();
  if (!git) return [];
  if (git.state !== "initialized") {
    await waitForEvent(git.onDidChangeState, (state) => state === "initialized", DISCOVERY_WAIT_MS);
  }
  if (git.repositories.length === 0) await waitForEvent(git.onDidOpenRepository, () => true, DISCOVERY_WAIT_MS);
  return git.repositories.map((r) => r.rootUri.fsPath);
}

function waitForEvent<T>(event: vscode.Event<T>, matches: (value: T) => boolean, timeoutMs: number): Promise<void> {
  return new Promise((resolve) => {
    const timer = setTimeout(done, timeoutMs);
    const subscription = event((value) => {
      if (matches(value)) done();
    });
    function done(): void {
      clearTimeout(timer);
      subscription.dispose();
      resolve();
    }
  });
}

// The workspace's git repository; a quick pick when there are several.
export async function pickRepository(): Promise<string | undefined> {
  const roots = await repositoryRoots();
  if (roots.length === 0) {
    showError("Open a git repository to start a review.");
    return undefined;
  }
  if (roots.length === 1) return roots[0];
  return vscode.window.showQuickPick(roots, { placeHolder: "Which repository do you want to review?" });
}

// The innermost of these repositories that holds a file (a nested repo wins).
export function repoContaining(uri: vscode.Uri, roots: string[]): string | undefined {
  return roots.filter((r) => reviewLocation(uri, r)).sort((a, b) => b.length - a.length)[0];
}

// Which reviewed file, and which side of its diff, an editor document is. The git
// extension's old side (toGitUri) keeps the file's own path; anything outside the
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

// The commit the review diff's old side is: HEAD, or a pull request's merge-base.
export function baseRef(repoRoot: string): string {
  return prReviewFor(repoRoot)?.baseSha ?? "HEAD";
}

// The reverse: the document a file's side opens as in the review diff.
export async function reviewUri(repoRoot: string, filePath: string, side: "new" | "old"): Promise<vscode.Uri> {
  const fileUri = vscode.Uri.file(path.join(repoRoot, filePath));
  if (side === "new") return fileUri;
  const git = await gitApi();
  return git ? git.toGitUri(fileUri, baseRef(repoRoot)) : fileUri;
}
