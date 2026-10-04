// The coding agent (always the scripted fake, tests/fake_acp_agent.py "comment"):
// choosing it, Look deeper, and Act Now's propose → apply / discard.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import * as path from "node:path";
import * as vscode from "vscode";

import {
  ensureReviewStarted,
  fromChat,
  mark,
  nextServerMessage,
  posted,
  probe,
  repo,
  serverMessages,
  waitFor,
} from "./helpers.ts";

const serverMessagesWithActNow = async (from: number): Promise<Record<string, unknown>[]> =>
  (await serverMessages("service_status", from)).filter((s) => "act_now" in s);

interface ActState {
  active: boolean;
  busy: boolean;
  proposal?: { files: { file_path: string; status: string }[]; summary: string };
}

const actState = (): Promise<ActState> => probe<ActState>("actNow.state");
const proposalTabs = (): vscode.Tab[] =>
  vscode.window.tabGroups.all
    .flatMap((g) => g.tabs)
    .filter((t) => t.input instanceof vscode.TabInputTextDiff && t.input.modified.scheme === "pear-proposal");

describe("coding agent", () => {
  before(ensureReviewStarted);

  it("turns off and on with Choose Coding Agent", async () => {
    // The test backend's config already selects the (fake) agent.
    const actNow = (s: Record<string, unknown>): { available: boolean } | undefined =>
      s.act_now as { available: boolean } | undefined;
    let from = await mark();
    await vscode.commands.executeCommand("pearReview.chooseAgent", "none");
    let status = await waitFor("Act Now to report off", async () =>
      (await serverMessagesWithActNow(from)).find((s) => actNow(s)?.available === false),
    );
    assert.ok(status);
    from = await mark();
    await vscode.commands.executeCommand("pearReview.chooseAgent", "cline");
    status = await waitFor("Act Now to report on", async () =>
      (await serverMessagesWithActNow(from)).find((s) => actNow(s)?.available === true),
    );
    assert.ok(status);
  });

  it("looks deeper at a hunk, read-only", async () => {
    await vscode.commands.executeCommand("pearReview.jumpToHunk", 2);
    const from = await mark();
    await fromChat({ kind: "lookDeeper", index: 2 });
    const answer = await nextServerMessage("deeper_turn", from);
    assert.match(String(answer.text), /nothing was changed/);
    assert.equal(answer.file_path, "sample.py");
  });

  it("proposes a change in act mode, shown as diffs", async () => {
    await fromChat({ kind: "setActMode", on: true });
    await waitFor("act mode", async () => (await actState()).active);
    await fromChat({ kind: "actNow", text: "Add a comment at the top" });
    const state = await waitFor("a proposal", async () => {
      const s = await actState();
      return s.proposal ? s : undefined;
    });
    assert.equal(state.active, false, "act mode is used up by one instruction");
    assert.deepEqual(
      state.proposal?.files.map((f) => [f.file_path, f.status]),
      [["sample.py", "modified"]],
    );
    await waitFor("the proposal's diff tab", () => proposalTabs().length > 0);
    await waitFor("the proposal's notification", async () =>
      (await probe<{ message: string }[]>("notices.shown")).some((n) =>
        /Cline proposes a change to sample\.py/.test(n.message),
      ),
    );
    assert.ok(!(await posted()).some((m) => m.kind === "proposal"), "the proposal stays out of the chat");
  });

  it("applies the proposal to the working tree", async () => {
    await vscode.commands.executeCommand("pearReview.actNow.apply");
    await waitFor("the applied notice", async () =>
      (await probe<{ message: string }[]>("notices.shown")).some((n) => /^Applied the change/.test(n.message)),
    );
    const sample = readFileSync(path.join(repo, "sample.py"), "utf8");
    assert.ok(sample.startsWith("# Reviewed with Act Now"), `sample.py was not changed:\n${sample}`);
    await waitFor("the proposal to clear", async () => !(await actState()).proposal);
    await waitFor("its diff tabs to close", () => proposalTabs().length === 0);
  });

  it("discards a proposal without touching the file", async () => {
    const before = readFileSync(path.join(repo, "sample.py"), "utf8");
    await fromChat({ kind: "setActMode", on: true });
    await fromChat({ kind: "actNow", text: "Add another comment" });
    await waitFor("a second proposal", async () => (await actState()).proposal);
    await vscode.commands.executeCommand("pearReview.actNow.discard");
    assert.equal((await actState()).proposal, undefined);
    await waitFor("its diff tabs to close", () => proposalTabs().length === 0);
    assert.equal(readFileSync(path.join(repo, "sample.py"), "utf8"), before);
  });

  it("locks the proposal while it's being refined", async () => {
    const before = readFileSync(path.join(repo, "sample.py"), "utf8");
    await fromChat({ kind: "setActMode", on: true });
    await fromChat({ kind: "actNow", text: "Add a third comment" });
    await waitFor("a proposal", async () => {
      const s = await actState();
      return s.proposal && !s.busy;
    });
    await vscode.commands.executeCommand("pearReview.actNow.refine", "Make it shorter");
    assert.equal((await actState()).busy, true, "refining locks the proposal");
    // Apply, as from the notification or the diff's title bar, while the agent refines.
    await vscode.commands.executeCommand("pearReview.actNow.apply");
    await vscode.commands.executeCommand("pearReview.actNow.discard");
    assert.ok((await actState()).proposal, "discard waits for the refine too");
    await waitFor("the refine to answer", async () => !(await actState()).busy);
    assert.equal(readFileSync(path.join(repo, "sample.py"), "utf8"), before, "nothing was applied mid-refine");
    await vscode.commands.executeCommand("pearReview.actNow.discard");
    assert.equal((await actState()).proposal, undefined);
  });
});
