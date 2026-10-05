import * as path from "node:path";
import * as vscode from "vscode";

import type { BackendManager } from "../backend/manager.ts";
import { gitApi, pickRepository, repositoryRoots } from "../git.ts";
import { GithubClient, GithubError, type PullRequest } from "../github/api.ts";
import { addPrReview, prReviewFor, removePrReview, type PrReview } from "../github/prReviews.ts";
import { toGithubComment, type ReviewEvent } from "../github/reviewComments.ts";
import {
  addWorktree,
  fetchPull,
  githubRemotes,
  removeStaleWorktrees,
  removeWorktree,
  type GithubRemote,
} from "../github/worktree.ts";
import { log, showError } from "../log.ts";
import { publish } from "../testProbe.ts";
import type { Comments } from "./comments.ts";

// Reviewing a GitHub pull request: the PR's head is checked out in a detached worktree
// under the extension's storage (the reviewer's own checkout is never touched), and a
// backend runs on it diffing against the merge-base, read-only. Comments queue as usual
// and are posted to the PR as one review instead of becoming a plan.
export function register(
  context: vscode.ExtensionContext,
  manager: BackendManager,
  comments: Comments,
): vscode.Disposable[] {
  const worktreeRoot = path.join(context.globalStorageUri.fsPath, "pr-worktrees");
  // Nothing is under review yet at activation, so anything here was left by a session
  // that ended without End Pull Request Review.
  void removeStaleWorktrees(worktreeRoot, () => false).catch((err: unknown) =>
    log(`Couldn't clear old pull request worktrees: ${String(err)}`),
  );

  const setReadOnly = (repo: string | undefined): void => {
    void vscode.commands.executeCommand("setContext", "pearReview.readOnly", prReviewFor(repo) !== undefined);
  };
  publish("pr.review", () => prReviewFor(manager.repoPath));

  // The integration tests' token stands in for the GitHub sign-in, which they can't do.
  const client = async (): Promise<GithubClient> => {
    const testToken = process.env.PEAR_REVIEW_GITHUB_TOKEN;
    if (testToken) return new GithubClient(testToken);
    const session = await vscode.authentication.getSession("github", ["repo"], { createIfNone: true });
    return new GithubClient(session.accessToken);
  };

  // `preset`: { repo?, remote?, number? } skips the pickers, for a keybinding or the tests.
  const reviewPullRequest = async (preset?: unknown): Promise<void> => {
    const given = (typeof preset === "object" && preset !== null ? preset : {}) as {
      repo?: unknown;
      remote?: unknown;
      number?: unknown;
    };
    // A PR's worktree is a repository too, once open; a PR isn't reviewed from one.
    const sources = (await repositoryRoots()).filter((root) => !prReviewFor(root));
    const repo =
      typeof given.repo === "string"
        ? given.repo
        : sources.length > 1
          ? await vscode.window.showQuickPick(sources, { placeHolder: "Which repository's pull requests?" })
          : (sources[0] ?? (await pickRepository()));
    if (!repo) return;

    const remotes = await githubRemotes(repo);
    const remote = await pickRemote(remotes, typeof given.remote === "string" ? given.remote : undefined);
    if (!remote) {
      if (!remotes.length) showError(`${path.basename(repo)} has no GitHub remote. Pull request reviews need one.`);
      return;
    }

    const github = await client();
    const pull =
      typeof given.number === "number"
        ? await github.getPull(remote.owner, remote.repo, given.number)
        : await pickPull(github, remote);
    if (!pull) return;

    const worktree = path.join(worktreeRoot, `${remote.repo}-pr-${pull.number}`);
    const previous = prReviewFor(worktree);
    if (previous) {
      // Reviewing the same PR again starts over from its current head.
      await manager.stopRepo(worktree);
      removePrReview(worktree);
    }
    const review = await vscode.window.withProgress(
      { location: vscode.ProgressLocation.Notification, title: `Pear Review: fetching PR #${pull.number}` },
      async (): Promise<PrReview> => {
        const baseSha = await fetchPull(repo, remote.name, {
          number: pull.number,
          headSha: pull.head.sha,
          baseRef: pull.base.ref,
          baseSha: pull.base.sha,
        });
        await addWorktree(repo, worktree, pull.head.sha);
        return {
          owner: remote.owner,
          repo: remote.repo,
          number: pull.number,
          title: pull.title,
          url: pull.html_url,
          headSha: pull.head.sha,
          baseSha,
          worktree,
          sourceRepo: repo,
        };
      },
    );
    addPrReview(review);
    log(
      `Reviewing PR #${review.number} (${review.headSha.slice(0, 7)} against ${review.baseSha.slice(0, 7)}) in ${worktree}`,
    );
    // So the diff's old side (a git: URI at the merge-base) can be read.
    await (await gitApi())?.openRepository(vscode.Uri.file(worktree));
    await vscode.window.withProgress(
      { location: vscode.ProgressLocation.Window, title: "Pear Review: starting backend" },
      () => manager.start(worktree),
    );
    setReadOnly(worktree);
    await vscode.commands.executeCommand("pearReview.startReview");
  };

  // `preset`: { event?, body? } skips the prompts.
  const submitReview = async (preset?: unknown): Promise<void> => {
    const review = prReviewFor(manager.repoPath);
    if (!review) {
      void vscode.window.showInformationMessage(
        "Submit Review is for a pull request review. Start one with Review Pull Request.",
      );
      return;
    }
    const given = (typeof preset === "object" && preset !== null ? preset : undefined) as
      { event?: unknown; body?: unknown } | undefined;
    const queued = comments.pending;
    const n = queued.length;
    const event: ReviewEvent | undefined = given
      ? isEvent(given.event)
        ? given.event
        : "COMMENT"
      : (
          await vscode.window.showQuickPick(
            [
              { label: "Comment", description: "Feedback without a verdict", event: "COMMENT" as const },
              {
                label: "Request changes",
                description: "Must be addressed before merging",
                event: "REQUEST_CHANGES" as const,
              },
              { label: "Approve", description: "Ready to merge", event: "APPROVE" as const },
            ],
            {
              title: `Submit review of PR #${review.number} (${n} comment${n === 1 ? "" : "s"})`,
              ignoreFocusOut: true,
            },
          )
        )?.event;
    if (!event) return;
    // GitHub refuses a comment-only or changes-requested review with nothing in it.
    const needsBody = event === "REQUEST_CHANGES" || (event === "COMMENT" && n === 0);
    const body = given
      ? typeof given.body === "string"
        ? given.body
        : ""
      : await vscode.window.showInputBox({
          title: `Review summary for PR #${review.number}`,
          prompt: needsBody ? "A summary (required for this kind of review)" : "A summary (optional)",
          ignoreFocusOut: true,
          validateInput: (value) =>
            needsBody && !value.trim() ? "GitHub needs a summary for this review." : undefined,
        });
    if (body === undefined) return;

    const github = await client();
    const result = await vscode.window.withProgress(
      {
        location: vscode.ProgressLocation.Notification,
        title: `Pear Review: posting the review to PR #${review.number}`,
      },
      async () => {
        const files = await github.listFiles(review.owner, review.repo, review.number);
        const patches = new Map(files.map((f) => [f.filename, f.patch]));
        const post = (lines: ReadonlyMap<string, string | undefined>) =>
          github.createReview(review.owner, review.repo, review.number, {
            commit_id: review.headSha,
            event,
            body: body.trim(),
            comments: queued.map((c) => toGithubComment(c, lines)),
          });
        try {
          return await post(patches);
        } catch (err) {
          // A line GitHub won't take inline (its diff can differ from ours, e.g. after a
          // force-push): post everything at file level rather than lose the review.
          if (!(err instanceof GithubError) || !/\bline\b/i.test(err.message)) throw err;
          log(`GitHub refused an inline comment (${err.message}); posting them as file comments.`);
          return post(new Map());
        }
      },
    );
    // Posted: out of the queue, so they can't be posted twice.
    for (const c of queued) manager.send("remove_review_comment", { id: c.id });
    const choice = await vscode.window.showInformationMessage(
      `Review posted to PR #${review.number} with ${n} comment${n === 1 ? "" : "s"}.`,
      "Open on GitHub",
      "End Review",
    );
    if (choice === "Open on GitHub") void vscode.env.openExternal(vscode.Uri.parse(result.html_url));
    else if (choice === "End Review") await endReview({ confirm: false });
  };

  // `preset`: { confirm: false } skips the unposted-comments question.
  const endReview = async (preset?: unknown): Promise<void> => {
    const review = prReviewFor(manager.repoPath);
    if (!review) return;
    const ask = !(typeof preset === "object" && preset !== null && (preset as { confirm?: unknown }).confirm === false);
    const n = comments.pending.length;
    if (ask && n > 0) {
      const choice = await vscode.window.showWarningMessage(
        `${n} comment${n === 1 ? " hasn't" : "s haven't"} been posted to PR #${review.number}. End the review anyway?`,
        { modal: true },
        "Discard and End",
      );
      if (choice !== "Discard and End") return;
    }
    await manager.stopRepo(review.worktree);
    removePrReview(review.worktree);
    setReadOnly(manager.repoPath);
    await removeWorktree(review.worktree);
    log(`Ended the review of PR #${review.number}.`);
  };

  const guarded =
    (fn: (preset?: unknown) => Promise<void>) =>
    (preset?: unknown): Promise<void> =>
      fn(preset).catch((err: unknown) => showError(err instanceof Error ? err.message : String(err)));

  return [
    manager.onDidChangeActiveRepo(setReadOnly),
    vscode.commands.registerCommand("pearReview.reviewPullRequest", guarded(reviewPullRequest)),
    vscode.commands.registerCommand("pearReview.submitPullRequestReview", guarded(submitReview)),
    vscode.commands.registerCommand("pearReview.endPullRequestReview", guarded(endReview)),
  ];
}

function isEvent(value: unknown): value is ReviewEvent {
  return value === "COMMENT" || value === "REQUEST_CHANGES" || value === "APPROVE";
}

// The remote whose pull requests to list. With a fork, "upstream" is usually where the
// PRs are, so it's offered first.
async function pickRemote(remotes: GithubRemote[], preset: string | undefined): Promise<GithubRemote | undefined> {
  if (preset) return remotes.find((r) => r.name === preset);
  if (remotes.length <= 1) return remotes[0];
  const ordered = [...remotes].sort((a, b) => Number(b.name === "upstream") - Number(a.name === "upstream"));
  const picked = await vscode.window.showQuickPick(
    ordered.map((r) => ({ label: `${r.owner}/${r.repo}`, description: r.name, remote: r })),
    { placeHolder: "Pull requests from which repository?" },
  );
  return picked?.remote;
}

async function pickPull(github: GithubClient, remote: GithubRemote): Promise<PullRequest | undefined> {
  const pulls = await vscode.window.withProgress(
    {
      location: vscode.ProgressLocation.Window,
      title: `Pear Review: listing ${remote.owner}/${remote.repo} pull requests`,
    },
    () => github.listOpenPulls(remote.owner, remote.repo),
  );
  if (!pulls.length) {
    void vscode.window.showInformationMessage(`${remote.owner}/${remote.repo} has no open pull requests.`);
    return undefined;
  }
  const picked = await vscode.window.showQuickPick(
    pulls.map((p) => ({
      label: `#${p.number} ${p.title}`,
      description: `${p.user?.login ?? ""}${p.draft ? " · draft" : ""}`,
      detail: `${p.head.label} → ${p.base.ref}`,
      pull: p,
    })),
    { placeHolder: "Pull request to review", matchOnDescription: true, matchOnDetail: true },
  );
  return picked?.pull;
}
