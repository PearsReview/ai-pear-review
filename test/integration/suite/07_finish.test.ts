// The end of a review: a spoken comment from the comment box's mic, Review all, the
// summary it ends on, and starting a new review.
import assert from "node:assert/strict";
import * as vscode from "vscode";

import {
  ensureReviewStarted,
  mark,
  nextServerMessage,
  posted,
  probe,
  repoUri,
  scriptFake,
  serverMessages,
  waitFor,
} from "./helpers.ts";

interface Tree {
  description?: string;
  reviewStarted: boolean;
}

interface Thread {
  body: string;
  severity: string;
  startLine: number;
}

describe("finishing a review", () => {
  before(ensureReviewStarted);

  it("adds a spoken comment from the comment box's mic", async () => {
    await scriptFake({ transcript: "Please add a docstring" });
    // A stand-in for the empty thread VS Code passes the comment box's title button.
    const thread = {
      uri: repoUri("calc.py"),
      range: new vscode.Range(1, 0, 1, 0),
      dispose: () => undefined,
    } as unknown as vscode.CommentThread;
    const from = await mark();
    await vscode.commands.executeCommand("pearReview.comment.voiceStart", thread);
    await waitFor("recording to start", async () =>
      (await posted()).slice(from).some((m) => m.kind === "recording" && m.recording === true),
    );
    await new Promise((resolve) => setTimeout(resolve, 800));
    await vscode.commands.executeCommand("pearReview.comment.voiceStop");
    const queued = await waitFor("the spoken comment's thread", async () =>
      (await probe<Thread[]>("comments.threads")).find((t) => t.body === "Please add a docstring"),
    );
    assert.equal(queued.severity, "suggestion");
    assert.equal(queued.startLine, 2);
    const replies = (await serverMessages("human_turn", from)).length;
    assert.equal(replies, 0, "the recording became a comment, not a chat question");
  });

  it("leaves a later chat recording alone", async () => {
    await scriptFake({ transcript: "What does this do?" });
    const from = await mark();
    await vscode.commands.executeCommand("pearReview.toggleRecording");
    await waitFor("recording to start", async () =>
      (await posted()).slice(from).some((m) => m.kind === "recording" && m.recording === true),
    );
    await new Promise((resolve) => setTimeout(resolve, 800));
    await vscode.commands.executeCommand("pearReview.toggleRecording");
    const human = await nextServerMessage("human_turn", from);
    assert.equal(human.text, "What does this do?");
  });

  it("marks everything reviewed, which ends the review on its summary", async () => {
    const from = await mark();
    await vscode.commands.executeCommand("pearReview.reviewAll");
    const summary = await nextServerMessage("presenting", from, (p) => p.ended === true);
    assert.equal(summary.reviewed_count, summary.total);
    assert.equal(summary.pending_comment_count, 1);
    assert.equal(summary.ended_early, false);
  });

  it("starts a new review from the summary", async () => {
    await vscode.commands.executeCommand("pearReview.newReview", { confirmed: true });
    await waitFor("a fresh review", async () => {
      const tree = await probe<Tree>("tree.view");
      return tree.reviewStarted && /^0\//.test(tree.description ?? "");
    });
  });
});
