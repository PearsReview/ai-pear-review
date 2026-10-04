// A multi-root workspace with two repositories (runTest.ts's "multi" run): each gets
// its own backend and its own .review/, switching keeps both running and resumes each
// one's review, and closing a repository stops its backend.
import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import * as path from "node:path";
import * as vscode from "vscode";

import { probe, repo, waitFor } from "./helpers.ts";

const second = process.env.PEAR_TEST_REPO_B ?? "";

interface Backends {
  shown: string | undefined;
  state: string;
  running: string[];
}

interface Tree {
  description: string | undefined;
  reviewStarted: boolean;
  files: { label: string }[];
}

const same = (a: string | undefined, b: string): boolean => !!a && path.relative(a, b) === "";
const backends = (): Promise<Backends> => probe<Backends>("backends");
const tree = (): Promise<Tree> => probe<Tree>("tree.view");

async function show(root: string): Promise<Tree> {
  await vscode.commands.executeCommand("pearReview.switchRepository", root);
  await waitFor(`${path.basename(root)} to be shown`, async () => {
    const b = await backends();
    return same(b.shown, root) && b.state === "ready";
  });
  return waitFor(`${path.basename(root)}'s changes in the tree`, async () => {
    const t = await tree();
    return t.files.length ? t : undefined;
  });
}

describe("two repositories in one window", () => {
  it("finds both, and opens neither until one is chosen", async () => {
    const roots = await waitFor("both repositories", async () => {
      const list = await probe<string[]>("repos.roots");
      return list.length === 2 ? list : undefined;
    });
    assert.ok(roots.some((r) => same(r, repo)) && roots.some((r) => same(r, second)));
    assert.equal((await backends()).running.length, 0, "no backend starts on its own with two repositories");
  });

  it("reviews the first in its own backend and .review/", async () => {
    const t = await show(repo);
    assert.equal(t.files.length, 5);
    assert.match(t.description ?? "", new RegExp(`^${path.basename(repo)}`), "the view names the repository");
    await vscode.commands.executeCommand("pearReview.startReview");
    await waitFor("the review to start", async () => (await tree()).reviewStarted, 60_000);
    await waitFor("the first repository's review state", () => existsSync(path.join(repo, ".review")));
  });

  it("switches to the second, keeping the first running", async () => {
    const t = await show(second);
    assert.deepEqual(
      t.files.map((f) => f.label),
      ["tools.py"],
    );
    assert.equal(t.reviewStarted, false, "the second repository's review hasn't started");
    const b = await backends();
    assert.equal(b.running.length, 2, "both backends run");
  });

  it("switches back and resumes the first review where it was", async () => {
    const t = await show(repo);
    const labels = t.files.map((f) => f.label);
    assert.ok(
      labels.includes("calc.py") && !labels.includes("tools.py"),
      `the first repo's changes: ${labels.join(", ")}`,
    );
    await waitFor("the resumed review", async () => (await tree()).reviewStarted);
  });

  it("stops a repository's backend when its folder is closed", async () => {
    const index = vscode.workspace.workspaceFolders?.findIndex((f) => same(f.uri.fsPath, second)) ?? -1;
    assert.ok(index > 0, "the second repository is a later workspace folder");
    vscode.workspace.updateWorkspaceFolders(index, 1);
    await waitFor(
      "the second backend to stop",
      async () => !(await backends()).running.some((r) => same(r, second)),
      30_000,
    );
    assert.ok(same((await backends()).shown, repo), "the first repository is still shown");
  });
});
