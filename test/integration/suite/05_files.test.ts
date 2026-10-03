// Beyond the changes: asking about any file (explore_reply), and reading a markdown file
// aloud with the passage being read highlighted.
import assert from "node:assert/strict";
import * as vscode from "vscode";

import {
  clearSelection,
  ensureReviewStarted,
  fromChat,
  live,
  mark,
  modelPrompts,
  nextServerMessage,
  posted,
  probe,
  repoUri,
  selectLines,
  waitFor,
} from "./helpers.ts";

describe("ask about a file", () => {
  before(ensureReviewStarted);

  it("points the chat at an unchanged file", async () => {
    await vscode.commands.executeCommand("pearReview.askAboutFile", repoUri("NOTES.md"));
    assert.equal(await probe("chat.target"), "NOTES.md");
    await waitFor("the chat banner", async () =>
      (await posted()).some((m) => m.kind === "target" && m.file_path === "NOTES.md"),
    );
  });

  it("answers a question about it, with selected lines as context", async () => {
    await selectLines("NOTES.md", 5, 7);
    const from = await mark();
    await fromChat({ kind: "send", text: "What is the usage section for?" });
    const human = await nextServerMessage("human_turn", from);
    assert.equal(human.index, -1, "a file question isn't about a hunk");
    assert.equal(human.file_path, "NOTES.md");
    const reply = await nextServerMessage("reviewer_turn", from);
    assert.equal(reply.index, -1);
    if (!live) assert.match(await modelPrompts(), /Regarding lines 5-7 of NOTES\.md/);
  });

  it("goes back to the review", async () => {
    await fromChat({ kind: "backToReview" });
    assert.equal(await probe("chat.target"), undefined);
  });
});

describe("read aloud", () => {
  before(ensureReviewStarted);

  it("reads a markdown file through the chat's audio", async () => {
    await vscode.window.showTextDocument(repoUri("NOTES.md"));
    clearSelection();
    const from = await mark();
    await vscode.commands.executeCommand("pearReview.readAloud", repoUri("NOTES.md"));
    const clip = await nextServerMessage("file_audio_chunk", from);
    assert.equal(clip.file_path, "NOTES.md");
    assert.equal(clip.start_line, 1);
  });

  it("reads only the selected lines", async () => {
    await selectLines("NOTES.md", 5, 8);
    const from = await mark();
    await vscode.commands.executeCommand("pearReview.readAloud", repoUri("NOTES.md"));
    const clip = await nextServerMessage("file_audio_chunk", from);
    assert.equal(clip.start_line, 5);
  });

  it("highlights the passage the chat says it is playing", async () => {
    await vscode.window.showTextDocument(repoUri("NOTES.md"));
    await fromChat({ kind: "reading", file_path: "NOTES.md", start_line: 5, end_line: 8 });
    assert.deepEqual(await probe("files.reading"), { filePath: "NOTES.md", startLine: 5, endLine: 8 });
    await fromChat({ kind: "readingDone" });
    assert.equal(await probe("files.reading"), undefined);
  });
});
