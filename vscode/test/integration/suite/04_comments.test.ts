// Review comments and Create Plan. A comment is submitted through the same command the
// comment widget's Must fix / Suggestion / Nit buttons run, with a stand-in for the
// CommentReply VS Code passes; the thread that stays is drawn from the backend's queue.
import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import * as path from "node:path";
import * as vscode from "vscode";

import { ensureReviewStarted, probe, repo, repoUri, waitFor } from "./helpers.ts";

interface Thread {
  id: number;
  uri: string;
  startLine: number;
  endLine: number;
  body: string;
  author: string;
  severity: string;
  comment: { body: string };
}

const threads = (): Promise<Thread[]> => probe<Thread[]>("comments.threads");

function reply(uri: vscode.Uri, startLine: number, endLine: number, text: string): vscode.CommentReply {
  const thread = {
    uri,
    range: new vscode.Range(startLine - 1, 0, endLine - 1, 0),
    dispose: () => undefined,
  } as unknown as vscode.CommentThread;
  return { thread, text };
}

async function headUri(file: string): Promise<vscode.Uri> {
  const git = vscode.extensions.getExtension<{ getAPI(v: 1): { toGitUri(u: vscode.Uri, ref: string): vscode.Uri } }>(
    "vscode.git",
  );
  return (await git!.activate()).getAPI(1).toGitUri(repoUri(file), "HEAD");
}

describe("review comments", () => {
  before(ensureReviewStarted);

  it("queues a comment on working-file lines and draws its thread there", async () => {
    await vscode.commands.executeCommand(
      "pearReview.comment.mustFix",
      reply(repoUri("sample.py"), 5, 6, "farewell needs a docstring"),
    );
    const thread = await waitFor("the comment's thread", async () =>
      (await threads()).find((t) => t.body === "farewell needs a docstring"),
    );
    assert.equal(thread.severity, "must-fix");
    assert.equal(thread.author, "Must fix");
    assert.ok(thread.uri.startsWith("file:"));
    assert.deepEqual([thread.startLine, thread.endLine], [5, 6]);
  });

  it("anchors a comment on a removed line to the HEAD side", async () => {
    await vscode.commands.executeCommand(
      "pearReview.comment.nit",
      reply(await headUri("src/funcs.py"), 61, 61, "why was f20 removed?"),
    );
    const thread = await waitFor("the HEAD-side thread", async () =>
      (await threads()).find((t) => t.body === "why was f20 removed?"),
    );
    assert.ok(thread.uri.startsWith("git:"), `expected a HEAD-side thread, got ${thread.uri}`);
    assert.equal(thread.startLine, 61);
    assert.match(String((await probe<{ description?: string }>("tree.view")).description), /2 comments/);
  });

  it("edits a queued comment", async () => {
    const thread = (await threads()).find((t) => t.body === "farewell needs a docstring");
    assert.ok(thread);
    thread.comment.body = "farewell needs a one-line docstring";
    await vscode.commands.executeCommand("pearReview.comment.save", thread.comment);
    await waitFor("the edit to come back from the backend", async () =>
      (await threads()).some((t) => t.id === thread.id && t.body === "farewell needs a one-line docstring"),
    );
  });

  it("deletes a queued comment", async () => {
    const thread = (await threads()).find((t) => t.body === "why was f20 removed?");
    assert.ok(thread);
    await vscode.commands.executeCommand("pearReview.comment.delete", thread.comment);
    await waitFor("the thread to go", async () => !(await threads()).some((t) => t.id === thread.id));
  });

  it("writes the plan from the queued comments", async () => {
    await vscode.commands.executeCommand("pearReview.createPlan", {
      note: "From the integration tests",
      asSkill: false,
    });
    const reviewDir = path.join(repo, ".review");
    const plan = await waitFor("a review plan in .review/", () =>
      readdirSync(reviewDir).find((f) => /^review_.*\.md$/.test(f)),
    );
    const text = readFileSync(path.join(reviewDir, plan), "utf8");
    assert.match(text, /From the integration tests/);
    assert.match(text, /farewell needs a one-line docstring/);
    await waitFor("the threads to clear once handed off", async () => (await threads()).length === 0);
  });
});
