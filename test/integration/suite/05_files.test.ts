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

// Read aloud plays through the extension's own player (python/player.py, with the fake
// sounddevice taking real time), controlled from the file's speaker.
interface Read {
  filePath: string;
  status: "loading" | "playing" | "paused";
}
const readState = (): Promise<Read | undefined> => probe<Read | undefined>("files.read");
const reading = (): Promise<{ filePath: string; startLine: number; endLine: number } | undefined> =>
  probe("files.reading");

async function stopRead(): Promise<void> {
  await vscode.commands.executeCommand("pearReview.stopReading");
  await waitFor("the read to stop", async () => !(await readState()));
}

describe("read aloud", () => {
  before(ensureReviewStarted);
  afterEach(stopRead);

  it("plays a markdown file, and the speaker pauses, resumes and stops it", async () => {
    await vscode.window.showTextDocument(repoUri("NOTES.md"));
    clearSelection();
    await vscode.commands.executeCommand("pearReview.readAloud", repoUri("NOTES.md"));
    await waitFor("playing", async () => (await readState())?.status === "playing");
    const at = await waitFor("the passage being read", reading);
    assert.equal(at.filePath, "NOTES.md");
    await vscode.commands.executeCommand("pearReview.readAloud", repoUri("NOTES.md"));
    await waitFor("paused", async () => (await readState())?.status === "paused");
    await vscode.commands.executeCommand("pearReview.readAloud", repoUri("NOTES.md"));
    await waitFor("playing again", async () => (await readState())?.status === "playing");
    await vscode.commands.executeCommand("pearReview.stopReading");
    await waitFor("stopped", async () => !(await readState()));
    assert.equal(await reading(), undefined, "the highlight goes when the read stops");
  });

  it("reads only the selected lines", async () => {
    await selectLines("NOTES.md", 5, 8);
    await vscode.commands.executeCommand("pearReview.readAloud", repoUri("NOTES.md"));
    const at = await waitFor("the passage being read", reading);
    assert.ok(at.startLine >= 5, `read from line ${at.startLine}`);
  });

  it("highlights the passage in the open file as the player reaches it", async () => {
    const editor = await vscode.window.showTextDocument(repoUri("NOTES.md"));
    clearSelection();
    await vscode.commands.executeCommand("pearReview.readAloud", repoUri("NOTES.md"));
    const at = await waitFor("the passage being read", reading);
    await waitFor("the cursor on the passage", () => editor.selection.active.line === at.startLine - 1);
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
    clearSelection();
    await vscode.commands.executeCommand("pearReview.tree.readAloud", node);
    await waitFor("the file to be read", async () => (await readState())?.filePath === "NOTES.md");
  });

  it("reading from a preview opens the text beside it, cursor on the passage", async () => {
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
    const at = await waitFor("the passage being read", reading);
    await waitFor("the cursor on the passage", () => editor.selection.active.line === at.startLine - 1);
  });

  it("leaves the chat out of it", async () => {
    const from = await mark();
    await vscode.commands.executeCommand("pearReview.readAloud", repoUri("NOTES.md"));
    await waitFor("playing", async () => (await readState())?.status === "playing");
    const chat = (await posted()).slice(from).filter((m) => m.kind === "server");
    assert.deepEqual(
      chat.map((m) => m.message?.type).filter((t) => t !== "service_status"),
      [],
      "no read-aloud messages reach the chat",
    );
  });
});
