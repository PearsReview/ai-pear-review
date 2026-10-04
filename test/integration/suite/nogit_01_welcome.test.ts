// A folder that isn't a git repository (runTest.ts's "nogit" run): the Changes view's
// welcome asks for a repository, and no backend process starts.
import assert from "node:assert/strict";
import * as vscode from "vscode";

import { probe, waitFor } from "./helpers.ts";

interface Backends {
  shown: string | undefined;
  state: string;
  running: string[];
}

describe("a folder that isn't a repository", () => {
  it("finds no repository, so the welcome asks for one", async () => {
    // Discovery waits a few seconds for the git extension before giving up.
    await waitFor("discovery to finish", async () => (await probe<string[]>("repos.roots")).length === 0, 30_000);
    await vscode.commands.executeCommand("workbench.view.extension.pearReview");
  });

  it("starts no backend, even when the view is shown", async () => {
    const opened = await vscode.commands.executeCommand<boolean>("pearReview.openChanges", { quiet: true });
    assert.equal(opened, false);
    const b = await probe<Backends>("backends");
    assert.equal(b.state, "stopped");
    assert.deepEqual(b.running, []);
    assert.equal(b.shown, undefined);
  });
});
