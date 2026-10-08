import * as path from "node:path";
import * as vscode from "vscode";

import type { BackendManager } from "../backend/manager.ts";
import { GithubClient } from "../github/api.ts";
import {
  checkedOutPull,
  githubPrExtensionInstalled,
  openPullRequests,
  type CheckedOutPull,
} from "../github/githubPrExtension.ts";
import { addPrReview, prReviewFor, removePrReview, type PrReview } from "../github/prReviews.ts";
import { enterpriseHost, GITHUB_COM, type GithubHost } from "../github/remote.ts";
import { git, githubRemotes, removeStaleWorktrees, type GithubRemote } from "../github/worktree.ts";
import { log } from "../log.ts";
import { publish } from "../testProbe.ts";

// Reviewing a GitHub pull request. The GitHub Pull Requests extension owns it: checking
// the PR out (in place or in a worktree), the comments and the submitted review. Pear
// Review follows whatever PR a repository has checked out: its backend diffs that
// checkout against the PR's merge-base, read-only, so the walkthrough, explanations and
// chat cover exactly what GitHub's "Files changed" shows. Pear's own comments are off
// for a PR (ui/comments.ts); GitHub's are the ones that go to the PR.

// Long enough for the GitHub extension to activate and answer on a cold start, short
// enough that a missing answer doesn't hold up starting the review.
const DETECT_TIMEOUT_MS = 8_000;
// How long a start that found no PR keeps asking the GitHub extension: 10 × 3 s.
const LATE_PULL_INTERVAL_MS = 3_000;
const LATE_PULL_TRIES = 10;

export function register(context: vscode.ExtensionContext, manager: BackendManager): vscode.Disposable[] {
  // Worktrees Pear Review used to make for PR reviews itself; nothing makes them now.
  const oldWorktrees = path.join(context.globalStorageUri.fsPath, "pr-worktrees");
  void removeStaleWorktrees(oldWorktrees, () => false).catch((err: unknown) =>
    log(`Couldn't clear old pull request worktrees: ${String(err)}`),
  );

  const setReadOnly = (repo: string | undefined): void => {
    void vscode.commands.executeCommand("setContext", "pearReview.readOnly", prReviewFor(repo) !== undefined);
  };
  publish("pr.review", () => prReviewFor(manager.repoPath));

  // The integration tests stand in for the GitHub extension and the sign-in, which they
  // can't drive: PEAR_REVIEW_TEST_PULL names the checked-out PR as {owner, repo, number}.
  const findPull = async (repo: string): Promise<CheckedOutPull | undefined> => {
    const testPull = process.env.PEAR_REVIEW_TEST_PULL;
    if (testPull) return { title: "", url: "", ...(JSON.parse(testPull) as Omit<CheckedOutPull, "title" | "url">) };
    return checkedOutPull(repo);
  };
  const client = async (host: GithubHost): Promise<GithubClient> => {
    const testToken = process.env.PEAR_REVIEW_GITHUB_TOKEN;
    if (testToken) return new GithubClient(host, testToken);
    const session = await vscode.authentication.getSession(host.authProvider, ["repo"], { createIfNone: true });
    return new GithubClient(host, session.accessToken);
  };

  // What the repository has checked out, as a PR review (undefined: not a PR). The
  // merge-base is worked out here, from the PR's base on GitHub, the way GitHub's
  // "Files changed" does.
  const resolve = async (repo: string): Promise<PrReview | undefined> => {
    const pull = await findPull(repo);
    if (!pull) return undefined;
    const remotes = await githubRemotes(repo, githubHosts());
    const remote = matchingRemote(remotes, pull);
    if (!remote) {
      log(
        `${path.basename(repo)} has PR #${pull.number} checked out, but no GitHub remote for ${pull.owner}/${pull.repo}.`,
      );
      return undefined;
    }
    const github = await client(remote.host);
    const details = await github.getPull(pull.owner, pull.repo, pull.number);
    const headSha = await git(repo, ["rev-parse", "HEAD"]);
    const hasBase = await git(repo, ["cat-file", "-e", `${details.base.sha}^{commit}`]).then(
      () => true,
      () => false,
    );
    if (!hasBase) await git(repo, ["fetch", "--no-tags", remote.name, `refs/heads/${details.base.ref}`]);
    const baseSha = await git(repo, ["merge-base", details.base.sha, headSha]);
    return {
      host: remote.host,
      owner: pull.owner,
      repo: pull.repo,
      number: pull.number,
      title: pull.title || details.title,
      url: pull.url || details.html_url,
      headSha,
      baseSha,
      worktree: repo,
      sourceRepo: repo,
    };
  };

  // Records what the repository has checked out. True when that changed how its backend
  // should run (a PR appeared, went, or moved to another commit).
  const detect = async (repo: string): Promise<boolean> => {
    const before = prReviewFor(repo);
    const found = await withTimeout(resolve(repo), DETECT_TIMEOUT_MS).catch((err: unknown) => {
      log(`Couldn't check ${path.basename(repo)} for a checked-out pull request: ${String(err)}`);
      return before; // keep what was known rather than flip on a transient failure
    });
    if (found) addPrReview(found);
    else removePrReview(repo);
    if (found && !sameReview(before, found)) {
      log(
        `Reviewing PR #${found.number} (${found.headSha.slice(0, 7)} against ${found.baseSha.slice(0, 7)}) in ${repo}`,
      );
    }
    setReadOnly(manager.repoPath);
    return !sameReview(before, found);
  };

  // The checked-out PR can change under a running backend (checking out another PR,
  // or going back to a branch). Rechecked when the window regains focus and on the
  // command; a change restarts the repository's backend so it diffs the right thing.
  let checking = false;
  const recheck = async (repo = manager.repoPath): Promise<void> => {
    if (!repo || checking) return;
    checking = true;
    try {
      if (!(await detect(repo))) return;
      await manager.stopRepo(repo);
      await manager.start(repo);
      await vscode.commands.executeCommand("pearReview.startReview");
    } finally {
      checking = false;
    }
  };

  // Right after a window opens on a PR checkout (Checkout in Worktree opens one), the
  // GitHub extension is still working out which PR it is, and says "none" until then.
  // So a start that finds no PR keeps asking for a while, and switches over once one
  // turns up. Once per repository per session: a plain branch stops being asked about.
  const disposables: vscode.Disposable[] = [];
  const watched = new Set<string>();
  const watchForLatePull = (repo: string): void => {
    const key = process.platform === "win32" ? path.resolve(repo).toLowerCase() : path.resolve(repo);
    if (watched.has(key) || !githubPrExtensionInstalled()) return;
    watched.add(key);
    let tries = 0;
    const timer = setInterval(() => {
      tries += 1;
      if (prReviewFor(repo) || tries > LATE_PULL_TRIES) {
        clearInterval(timer);
        return;
      }
      void recheck(repo).catch((err: unknown) => log(`Pull request check failed: ${String(err)}`));
    }, LATE_PULL_INTERVAL_MS);
    disposables.push({ dispose: () => clearInterval(timer) });
  };

  manager.setBeforeStart(async (repo) => {
    await detect(repo);
    if (!prReviewFor(repo)) watchForLatePull(repo);
  });

  return [
    {
      dispose: () => {
        for (const d of disposables) d.dispose();
      },
    },
    manager.onDidChangeActiveRepo(setReadOnly),
    vscode.window.onDidChangeWindowState((state) => {
      if (state.focused) void recheck().catch((err: unknown) => log(`Pull request check failed: ${String(err)}`));
    }),
    vscode.commands.registerCommand("pearReview.reviewPullRequest", () =>
      openPullRequests().catch((err: unknown) => log(`Couldn't open GitHub's pull requests: ${String(err)}`)),
    ),
    vscode.commands.registerCommand("pearReview.refreshPullRequest", () => recheck()),
  ];
}

// github.com, plus the GitHub Enterprise host VS Code's github-enterprise.uri names.
function githubHosts(): GithubHost[] {
  const enterprise = enterpriseHost(vscode.workspace.getConfiguration("github-enterprise").get<string>("uri"));
  return enterprise ? [GITHUB_COM, enterprise] : [GITHUB_COM];
}

// The remote for the PR's repository; "upstream" first when a fork has both.
function matchingRemote(remotes: GithubRemote[], pull: CheckedOutPull): GithubRemote | undefined {
  const same = (a: string, b: string): boolean => a.toLowerCase() === b.toLowerCase();
  const matches = remotes.filter((r) => same(r.owner, pull.owner) && same(r.repo, pull.repo));
  return matches.find((r) => r.name === "upstream") ?? matches[0];
}

function sameReview(a: PrReview | undefined, b: PrReview | undefined): boolean {
  if (!a || !b) return a === b;
  return a.number === b.number && a.headSha === b.headSha && a.baseSha === b.baseSha;
}

function withTimeout<T>(promise: Promise<T>, ms: number): Promise<T> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`no answer within ${ms / 1000}s`)), ms);
    promise.then(
      (value) => {
        clearTimeout(timer);
        resolve(value);
      },
      (err: unknown) => {
        clearTimeout(timer);
        reject(err instanceof Error ? err : new Error(String(err)));
      },
    );
  });
}
