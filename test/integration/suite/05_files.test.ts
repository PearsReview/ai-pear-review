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

  it("opens a markdown file's preview, and reads it, from the Changes tree", async () => {
    // What a markdown row passes its inline buttons.
    const node = { kind: "file", file: { file_path: "NOTES.md", hunks: [] } };
    await vscode.commands.executeCommand("pearReview.tree.preview", node);
    await waitFor("the markdown preview", () =>
      vscode.window.tabGroups.all
        .flatMap((g) => g.tabs)
        .some((t) => t.input instanceof vscode.TabInputWebview && t.input.viewType.includes("markdown.preview")),
    );
    const from = await mark();
    clearSelection();
    await vscode.commands.executeCommand("pearReview.tree.readAloud", node);
    const clip = await nextServerMessage("file_audio_chunk", from);
    assert.equal(clip.file_path, "NOTES.md");
  });

  it("highlights the passage being read in the open file, end to end", async () => {
    await vscode.window.showTextDocument(repoUri("NOTES.md"));
    clearSelection();
    await vscode.commands.executeCommand("pearReview.chat.focus");
    await fromChat({ kind: "readingDone" });
    await vscode.commands.executeCommand("pearReview.readAloud", repoUri("NOTES.md"));
    // The chat panel itself reports the passage as its player takes the clip.
    const reading = await waitFor("the chat to report the passage", async () =>
      probe<{ filePath: string; startLine: number } | undefined>("files.reading"),
    );
    assert.equal(reading.filePath, "NOTES.md");
  });

  it("reading from a preview opens the text beside it, cursor on the passage", async () => {
    await fromChat({ kind: "readingDone" });
    await vscode.commands.executeCommand("workbench.action.closeAllEditors");
    await vscode.commands.executeCommand("markdown.showPreview", repoUri("NOTES.md"));
    // The preview command returns before its tab exists.
    await waitFor("the preview tab", () => {
      const tab = vscode.window.tabGroups.activeTabGroup.activeTab;
      return tab?.input instanceof vscode.TabInputWebview ? tab : undefined;
    });
    await vscode.commands.executeCommand("pearReview.readAloud");
    const editor = await waitFor("NOTES.md's text beside the preview", () =>
      vscode.window.visibleTextEditors.find((e) => e.document.uri.fsPath === repoUri("NOTES.md").fsPath),
    );
    const reading = await waitFor("the passage being read", async () =>
      probe<{ startLine: number } | undefined>("files.reading"),
    );
    await waitFor("the cursor on the passage", () => editor.selection.active.line === reading.startLine - 1);
  });

  it("highlights the passage the chat says it is playing", async () => {
    await vscode.window.showTextDocument(repoUri("NOTES.md"));
    await fromChat({ kind: "reading", file_path: "NOTES.md", start_line: 5, end_line: 8 });
    assert.deepEqual(await probe("files.reading"), { filePath: "NOTES.md", startLine: 5, endLine: 8 });
    await fromChat({ kind: "readingDone" });
    assert.equal(await probe("files.reading"), undefined);
  });
});
