// The end of a review: comments left from the chat (typed and spoken), Review all, the
// summary it ends on, and starting a new review.
import assert from "node:assert/strict";
import * as vscode from "vscode";

import {
  ensureReviewStarted,
  fromChat,
  mark,
  nextServerMessage,
  posted,
  probe,
  scriptFake,
  waitFor,
} from "./helpers.ts";

interface Tree {
  description?: string;
  reviewStarted: boolean;
}

describe("finishing a review", () => {
  before(ensureReviewStarted);

  it("turns a typed message into a comment in comment mode", async () => {
    await fromChat({ kind: "setCommentMode", on: true });
    await waitFor(
      "comment mode",
      async () => [...(await posted())].reverse().find((m) => m.kind === "commentMode")?.on === true,
    );
    const from = await mark();
    await fromChat({ kind: "comment", text: "Rename x to total" });
    const queued = await nextServerMessage("review_comment_queued", from);
    assert.equal(queued.instruction, "Rename x to total");
    assert.equal(queued.severity, "suggestion");
    await waitFor(
      "comment mode to end with the comment",
      async () => [...(await posted())].reverse().find((m) => m.kind === "commentMode")?.on === false,
    );
  });

  it("turns a spoken message into a comment, transcription included", async () => {
    await scriptFake({ transcript: "Please add a docstring" });
    await fromChat({ kind: "setCommentMode", on: true });
    const from = await mark();
    await vscode.commands.executeCommand("pearReview.toggleRecording");
    await waitFor("recording to start", async () =>
      (await posted()).slice(from).some((m) => m.kind === "recording" && m.recording === true),
    );
    await new Promise((resolve) => setTimeout(resolve, 800));
    await vscode.commands.executeCommand("pearReview.toggleRecording");
    const queued = await nextServerMessage("review_comment_queued", from);
    assert.equal(queued.instruction, "Please add a docstring");
  });

  it("marks everything reviewed, which ends the review on its summary", async () => {
    const from = await mark();
    await vscode.commands.executeCommand("pearReview.reviewAll");
    const summary = await nextServerMessage("presenting", from, (p) => p.ended === true);
    assert.equal(summary.reviewed_count, summary.total);
    assert.equal(summary.pending_comment_count, 2);
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
