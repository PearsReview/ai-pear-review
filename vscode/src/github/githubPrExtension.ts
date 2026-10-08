// The GitHub Pull Requests extension (GitHub.vscode-pull-request-github), which owns pull
// request reviews: checking a PR out (in place or in a worktree), its comments, threads
// and suggestions, and submitting the review. Pear Review only asks it which PR a
// repository has checked out, and adds explanations and chat on top.
import * as vscode from "vscode";

export const GITHUB_PR_EXTENSION_ID = "GitHub.vscode-pull-request-github";

// Its view container in the activity bar.
const PULL_REQUESTS_VIEW = "workbench.view.extension.github-pull-requests";

// The slice of the extension's exported API this uses. getRepositoryDescription
// answers for the repository containing a file: a PR is set while one is checked out.
interface GithubPrApi {
  getRepositoryDescription(uri: vscode.Uri): Promise<
    | {
        owner: string;
        repositoryName: string;
        defaultBranch: string;
        pullRequest?: { title: string; url: string; number: number; id: number };
      }
    | undefined
  >;
}

export interface CheckedOutPull {
  owner: string;
  repo: string;
  number: number;
  title: string;
  url: string;
}

export function githubPrExtensionInstalled(): boolean {
  return vscode.extensions.getExtension(GITHUB_PR_EXTENSION_ID) !== undefined;
}

async function api(): Promise<GithubPrApi | undefined> {
  const extension = vscode.extensions.getExtension<unknown>(GITHUB_PR_EXTENSION_ID);
  if (!extension) return undefined;
  const exported = extension.isActive ? extension.exports : await extension.activate();
  const candidate = exported as Partial<GithubPrApi> | undefined;
  return typeof candidate?.getRepositoryDescription === "function" ? (candidate as GithubPrApi) : undefined;
}

// The pull request checked out in this repository, if the extension reports one.
export async function checkedOutPull(repoRoot: string): Promise<CheckedOutPull | undefined> {
  const github = await api();
  const description = await github?.getRepositoryDescription(vscode.Uri.file(repoRoot));
  const pull = description?.pullRequest;
  if (!description || !pull) return undefined;
  return {
    owner: description.owner,
    repo: description.repositoryName,
    number: pull.number,
    title: pull.title,
    url: pull.url,
  };
}

// Where a pull request review starts: the extension's Pull Requests view, or an offer to
// install the extension when it's missing.
export async function openPullRequests(): Promise<void> {
  if (!githubPrExtensionInstalled()) {
    const choice = await vscode.window.showInformationMessage(
      "Pull request reviews use the GitHub Pull Requests extension for checkout and comments; " +
        "Pear Review adds explanations and chat on top. Install it?",
      "Install",
    );
    if (choice === "Install") {
      await vscode.commands.executeCommand("workbench.extensions.installExtension", GITHUB_PR_EXTENSION_ID);
    }
    return;
  }
  await vscode.commands.executeCommand(PULL_REQUESTS_VIEW);
  void vscode.window.showInformationMessage(
    "Pick the pull request in GitHub's Pull Requests view and choose Checkout (or Checkout in Worktree). " +
      "Pear Review follows the checked-out PR: comment and submit with GitHub's tools, and ask Pear about any change.",
  );
}
