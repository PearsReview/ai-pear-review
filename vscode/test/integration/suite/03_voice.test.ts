// Voice: press-to-start / press-to-stop recording through the backend (the fake
// sounddevice records a tone), transcription by the fake speech service, and the
// transcript sent as a reply.
import assert from "node:assert/strict";
import * as vscode from "vscode";

import {
  ensureReviewStarted,
  fakeRequests,
  mark,
  nextServerMessage,
  posted,
  probe,
  scriptFake,
  waitFor,
} from "./helpers.ts";

describe("voice", () => {
  before(ensureReviewStarted);

  it("records, transcribes and replies to a spoken question", async () => {
    await scriptFake({ transcript: "Is the docstring accurate?" });
    const from = await mark();
    await vscode.commands.executeCommand("pearReview.toggleRecording");
    await waitFor("recording to start", async () =>
      (await posted()).slice(from).some((m) => m.kind === "recording" && m.recording === true),
    );
    await new Promise((resolve) => setTimeout(resolve, 800));
    await vscode.commands.executeCommand("pearReview.toggleRecording");

    const human = await nextServerMessage("human_turn", from);
    assert.equal(human.text, "Is the docstring accurate?");
    await nextServerMessage("reviewer_turn", from);

    const uploads = (await fakeRequests()).filter((r) => r.path === "/transcribe");
    const last = uploads[uploads.length - 1];
    assert.ok(last?.body.wav, "the recording reaches speech-to-text as WAV");
  });

  it("rejects a recording too short to be speech", async () => {
    const from = await mark();
    await vscode.commands.executeCommand("pearReview.toggleRecording");
    await waitFor("recording to start", async () =>
      (await posted()).slice(from).some((m) => m.kind === "recording" && m.recording === true),
    );
    await vscode.commands.executeCommand("pearReview.toggleRecording");
    await waitFor("a too-short notice", async () =>
      (await probe<{ kind: string; message: string }[]>("notices.shown")).some((n) => /too short/i.test(n.message)),
    );
    assert.equal(from >= 0, true);
  });
});
