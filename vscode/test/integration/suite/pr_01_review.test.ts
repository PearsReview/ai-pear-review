// A GitHub pull request review (runTest.ts's "pr" run). The PR is checked out the way the
// GitHub Pull Requests extension leaves it, and PEAR_REVIEW_TEST_PULL stands in for that
// extension's answer to "which PR is checked out?". Pear Review follows it: the review
// is the PR's own changes against its merge-base (whose base comes from a fake GitHub
// API), nothing can be written, comments are left to GitHub's extension, and a change
// can be explained from the cursor.
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { readdirSync } from "node:fs";
import * as path from "node:path";
import * as vscode from "vscode";

import { fakeRequests, fromChat, mark, nextServerMessage, probe, repo, repoUri, waitFor } from "./helpers.ts";

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
const checkedOut = git(repo, "rev-parse", "HEAD");

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

  it("follows the checked-out PR and reviews it against the merge-base", async () => {
    await vscode.commands.executeCommand("pearReview.startReview");
    review = await waitFor("the PR review", () => probe<PrReview | undefined>("pr.review"), 90_000);
    assert.equal(review.number, 7);
    assert.equal(review.baseSha, mergeBase, "diffed against the merge-base, not main's tip");
    assert.equal(review.headSha, checkedOut);
    assert.equal(path.resolve(review.worktree).toLowerCase(), path.resolve(repo).toLowerCase());
    const base = (await fakeRequests()).find((r) => r.path.endsWith("/pulls/7"));
    assert.equal(base?.body.auth, "Bearer test-token", "the PR's base was asked of GitHub");
    const tree = await waitFor(
      "the review to start",
      async () => {
        const t = await probe<Tree>("tree.view");
        return t.reviewStarted && t.files.length ? t : undefined;
      },
      90_000,
    );
    // Only the PR's own changes: not main's later commit.
    assert.deepEqual(tree.files.map((f) => f.label).sort(), ["calc.py", "mul.py"]);
  });

  it("doesn't ask for briefings: a PR's own text says why", async () => {
    const status = await waitFor("the briefing status", () =>
      probe<{ out_of_date: number[] } | undefined>("briefings.status"),
    );
    assert.deepEqual(status.out_of_date, []);
    assert.deepEqual(await probe<string[]>("briefings.warnings"), []);
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

  it("leaves comments to the GitHub Pull Requests extension", async () => {
    await vscode.commands.executeCommand(
      "pearReview.comment.mustFix",
      reply(repoUri("calc.py"), 2, "Say what it returns."),
    );
    // Give a queued comment time to come back from the backend, were one sent.
    await new Promise((resolve) => setTimeout(resolve, 1_000));
    assert.deepEqual(await probe<unknown[]>("comments.threads"), []);
  });

  it("explains the change the cursor is in", async () => {
    const editor = await vscode.window.showTextDocument(repoUri("mul.py"), { preview: false });
    editor.selection = new vscode.Selection(0, 0, 0, 0);
    const from = await mark();
    await vscode.commands.executeCommand("pearReview.explainHere");
    const presenting = await nextServerMessage("presenting", from, (p) => p.file_path === "mul.py");
    await nextServerMessage("narration", from, (p) => p.index === presenting.index);
  });

  it("leaves the checkout as it was, and writes no plan", () => {
    assert.equal(git(repo, "rev-parse", "HEAD"), checkedOut);
    assert.equal(git(repo, "status", "--porcelain"), "");
    const handoffs = readdirSync(path.join(repo, ".review")).filter((f) => f.startsWith("review_"));
    assert.deepEqual(handoffs, [], "no plan was written");
  });
});
