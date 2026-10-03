// The chat: narration, a typed reply with the editor selection as context, and reading
// a reply aloud.
import assert from "node:assert/strict";
import * as vscode from "vscode";

import {
  ensureReviewStarted,
  fromChat,
  live,
  mark,
  modelPrompts,
  nextServerMessage,
  posted,
  selectLines,
  waitFor,
} from "./helpers.ts";

describe("chat", () => {
  before(ensureReviewStarted);

  it("narrates a hunk when the review reaches it, as markdown blocks", async () => {
    const from = await mark();
    await vscode.commands.executeCommand("pearReview.jumpToHunk", 2);
    const narration = await nextServerMessage("narration", from, (n) => n.index === 2);
    assert.equal(narration.file_path, "sample.py");
    assert.ok(String(narration.text).trim(), "the narration has text");
    assert.ok(Array.isArray(narration.blocks) && narration.blocks.length, "and render-ready blocks");
    if (!live) {
      const spans = (narration.blocks as { spans: { style: string; text: string }[] }[]).flatMap((b) => b.spans);
      assert.ok(
        spans.some((s) => s.style === "code" && s.text === "add"),
        "inline code becomes a code span",
      );
    }
  });

  it("sends a typed question with the selected lines as context", async () => {
    await selectLines("sample.py", 5, 6);
    const from = await mark();
    await fromChat({ kind: "send", text: "Why add farewell?" });

    const human = await nextServerMessage("human_turn", from);
    assert.equal(human.text, "Why add farewell?", "the transcript shows the question, not the context");
    const reply = await nextServerMessage("reviewer_turn", from);
    assert.ok(String(reply.text).trim());
    await waitFor("the selection to be used up", async () =>
      (await posted()).slice(from).some((m) => m.kind === "context" && m.label === null),
    );
    if (!live) {
      const prompts = await modelPrompts();
      assert.match(prompts, /Regarding lines 5-6 of sample\.py/);
      assert.match(prompts, /Why add farewell\?/);
    }
  });

  it("reads a reply aloud on request", async () => {
    const from = await mark();
    await fromChat({ kind: "speak", text: "It returns a goodbye message." });
    const clip = await nextServerMessage("turn_audio_chunk", from);
    assert.equal(clip.chunk_index, 0);
    assert.equal(clip.mime_type, "audio/wav");
  });

  it("explains on request", async () => {
    const from = await mark();
    await vscode.commands.executeCommand("pearReview.explain");
    const narration = await nextServerMessage("narration", from);
    assert.equal(narration.index, 2);
  });
});
