// A GitHub pull request review (runTest.ts's "pr" run): the PR is checked out in a
// worktree and diffed against its merge-base, nothing can be written, and the queued
// comments are posted to the PR (a fake GitHub API) as one review.
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { existsSync, readdirSync } from "node:fs";
import * as path from "node:path";
import * as vscode from "vscode";

import { fakeRequests, fromChat, probe, repo, waitFor } from "./helpers.ts";

interface PrReview {
  number: number;
  baseSha: string;
  headSha: string;
  worktree: string;
}

interface Tree {
  reviewStarted: boolean;
  files: { label: string }[];
}

const mergeBase = process.env.PEAR_TEST_PR_MERGE_BASE ?? "";
const git = (cwd: string, ...args: string[]): string => execFileSync("git", args, { cwd, encoding: "utf8" }).trim();
const sourceHead = git(repo, "rev-parse", "HEAD");

function reply(uri: vscode.Uri, line: number, text: string): vscode.CommentReply {
  const thread = {
    uri,
    range: new vscode.Range(line - 1, 0, line - 1, 0),
    dispose: () => undefined,
  } as unknown as vscode.CommentThread;
  return { thread, text };
}

describe("a pull request review", () => {
  let review: PrReview;

  it("checks the PR out in a worktree and reviews it against the merge-base", async () => {
    await vscode.commands.executeCommand("pearReview.reviewPullRequest", { repo, remote: "origin", number: 7 });
    review = await waitFor("the PR review", () => probe<PrReview | undefined>("pr.review"));
    assert.equal(review.number, 7);
    assert.equal(review.baseSha, mergeBase, "diffed against the merge-base, not main's tip");
    assert.equal(git(review.worktree, "rev-parse", "HEAD"), review.headSha);
    const tree = await waitFor(
      "the review to start",
      async () => {
        const t = await probe<Tree>("tree.view");
        return t.reviewStarted && t.files.length ? t : undefined;
      },
      90_000,
    );
    // Only the PR's own changes: not main's later commit, not the reviewer's own edit.
    assert.deepEqual(tree.files.map((f) => f.label).sort(), ["calc.py", "mul.py"]);
  });

  it("has no Act Now", async () => {
    await vscode.commands.executeCommand("pearReview.actNow.toggle");
    assert.equal((await probe<{ active: boolean }>("actNow.state")).active, false);
    await fromChat({ kind: "actNow", text: "Add type hints" });
    await waitFor("the backend to refuse it", async () =>
      (await probe<{ kind: string; message: string }[]>("notices.shown")).some(
        (n) => n.kind === "error" && /read-only/.test(n.message),
      ),
    );
  });

  it("posts the queued comments to the PR as one review", async () => {
    const file = (name: string): vscode.Uri => vscode.Uri.file(path.join(review.worktree, name));
    await vscode.commands.executeCommand(
      "pearReview.comment.mustFix",
      reply(file("calc.py"), 2, "Say what it returns."),
    );
    await vscode.commands.executeCommand(
      "pearReview.comment.nit",
      reply(file("calc.py"), 7, "sub could use a docstring too."),
    );
    await waitFor("both comments queued", async () => (await probe<unknown[]>("comments.threads")).length === 2);

    // Not awaited: it ends on a notification with buttons, which a test can't press.
    void vscode.commands.executeCommand("pearReview.submitPullRequestReview", {
      event: "REQUEST_CHANGES",
      body: "A couple of things.",
    });
    const posted = await waitFor("the review POST", async () =>
      (await fakeRequests()).find((r) => r.path.endsWith("/pulls/7/reviews")),
    );
    assert.equal(posted.body.auth, "Bearer test-token");
    assert.equal(posted.body.commit_id, review.headSha);
    assert.equal(posted.body.event, "REQUEST_CHANGES");
    assert.equal(posted.body.body, "A couple of things.");
    assert.deepEqual(posted.body.comments, [
      { path: "calc.py", body: "**Must fix:** Say what it returns.", line: 2, side: "RIGHT" },
      // Line 7 is outside GitHub's hunk, so it goes on the file.
      { path: "calc.py", body: "Line 7: **Nit:** sub could use a docstring too.", subject_type: "file" },
    ]);
    await waitFor(
      "the posted comments to leave the queue",
      async () => !(await probe<unknown[]>("comments.threads")).length,
    );
  });

  it("ends the review, removing the worktree, and the reviewer's checkout is untouched", async () => {
    await vscode.commands.executeCommand("pearReview.endPullRequestReview", { confirm: false });
    assert.equal(await probe("pr.review"), undefined);
    assert.equal(existsSync(review.worktree), false);
    assert.equal(git(repo, "rev-parse", "HEAD"), sourceHead);
    assert.equal(git(repo, "status", "--porcelain"), "M README.md");
    const handoffs = readdirSync(path.join(repo, ".review")).filter((f) => f.startsWith("review_"));
    assert.deepEqual(handoffs, [], "no plan was written");
  });
});
