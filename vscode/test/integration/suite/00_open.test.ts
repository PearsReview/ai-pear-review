// Before Start Review: opening the changes fills the tree and opens the diffs, and the
// chat answers questions. Only narration, reviewed marks and comments wait for the review.
import assert from "node:assert/strict";
import * as vscode from "vscode";

import { fromChat, mark, nextServerMessage, probe, waitFor } from "./helpers.ts";

interface Tree {
  files: unknown[];
  reviewStarted: boolean;
}

describe("before the review starts", () => {
  it("opens the changes without starting the review", async () => {
    await vscode.commands.executeCommand("pearReview.openChanges");
    const tree = await waitFor(
      "the Changes tree to fill",
      async () => {
        const t = await probe<Tree>("tree.view");
        return t.files.length ? t : undefined;
      },
      90_000,
    );
    assert.equal(tree.files.length, 5);
    assert.equal(tree.reviewStarted, false);
    await waitFor("the first hunk's diff", async () => await probe("diff.highlight"));
  });

  it("answers a question about the hunk on screen", async () => {
    const from = await mark();
    await fromChat({ kind: "send", text: "What does this do?" });
    const human = await nextServerMessage("human_turn", from);
    assert.equal(human.text, "What does this do?");
    await nextServerMessage("reviewer_turn", from);
    assert.equal((await probe<Tree>("tree.view")).reviewStarted, false, "asking doesn't start the review");
  });
});
