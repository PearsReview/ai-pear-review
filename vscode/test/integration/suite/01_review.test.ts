// The review loop: start, the Changes tree, the diff and its highlight, moving between
// hunks, and marking them reviewed. Hunk order is the backend's: calc.py, old.py
// (deleted), sample.py, src/funcs.py (removal only), new_module.py (untracked).
import assert from "node:assert/strict";
import * as vscode from "vscode";

import { ensureReviewStarted, probe, waitFor } from "./helpers.ts";

interface TreeItem {
  label: string;
  description?: string;
  contextValue?: string;
  icon?: string;
  hunks?: TreeItem[];
}
interface Tree {
  description?: string;
  files: TreeItem[];
}
interface Highlight {
  uri: string;
  lines: number[];
}

const tree = (): Promise<Tree> => probe<Tree>("tree.view");
const highlight = (): Promise<Highlight | undefined> => probe<Highlight | undefined>("diff.highlight");

async function currentHunk(): Promise<{ file: string; hunk: TreeItem } | undefined> {
  for (const file of (await tree()).files) {
    const hunk = file.hunks?.find((h) => h.contextValue === "hunk.current");
    if (hunk) return { file: file.label, hunk };
  }
  return undefined;
}

async function goTo(index: number, file: string): Promise<Highlight> {
  await vscode.commands.executeCommand("pearReview.jumpToHunk", index);
  await waitFor(`hunk ${index} (${file}) to be current`, async () => (await currentHunk())?.file === file);
  // The highlight is set before the diff tab finishes opening; wait for both.
  await waitFor(`the diff tab for ${file}`, () => {
    const input = vscode.window.tabGroups.activeTabGroup.activeTab?.input;
    return (
      input instanceof vscode.TabInputTextDiff &&
      [input.original, input.modified].some((u) => decodeURIComponent(u.toString()).includes(file))
    );
  });
  return waitFor(`the diff highlight for ${file}`, async () => {
    const h = await highlight();
    return h && decodeURIComponent(h.uri).includes(file) ? h : undefined;
  });
}

describe("review loop", () => {
  before(ensureReviewStarted);

  it("lists every changed file and its hunks in the Changes tree", async () => {
    const files = (await tree()).files;
    assert.deepEqual(files.map((f) => f.label).sort(), ["calc.py", "funcs.py", "new_module.py", "old.py", "sample.py"]);
    const calc = files.find((f) => f.label === "calc.py");
    assert.equal(calc?.hunks?.[0]?.label, "Lines 1–3");
    assert.match((await tree()).description ?? "", /0\/5 reviewed/);
  });

  it("warns that no change has a briefing, and marks each one's row", async () => {
    const warnings = await waitFor("the briefing warning", async () => {
      const shown = await probe<string[]>("briefings.warnings");
      return shown.length ? shown : undefined;
    });
    assert.match(warnings[0] ?? "", /5 of 5 changes have no up-to-date briefing/);
    assert.match(warnings[0] ?? "", /use the prep-review skill/);
    // One per installed assistant (Claude Code, Cline), or a copy when neither is.
    const buttons = await probe<string[]>("briefings.buttons");
    assert.ok(buttons.length && buttons.every((b) => /^Brief in |^Copy Instruction$/.test(b)), buttons.join(", "));
    const rows = (await tree()).files.flatMap((f) => f.hunks ?? []);
    assert.ok(
      rows.every((h) => /not briefed/.test(h.description ?? "")),
      "every row is marked",
    );
  });

  it("opens the first hunk as a diff with its added line highlighted", async () => {
    const h = await goTo(0, "calc.py");
    assert.ok(h.uri.startsWith("file:"), "added lines are highlighted on the working-file side");
    assert.deepEqual(h.lines, [2]);
    const tab = vscode.window.tabGroups.activeTabGroup.activeTab;
    assert.ok(tab?.input instanceof vscode.TabInputTextDiff, "the hunk opens as a diff tab");
    assert.equal(tab.input.original.scheme, "git", "the left side is HEAD");
  });

  it("highlights a removal-only hunk on the HEAD side", async () => {
    const h = await goTo(3, "funcs.py");
    assert.ok(h.uri.startsWith("git:"), `expected the HEAD side, got ${h.uri}`);
    assert.deepEqual(h.lines, [61, 62, 63]);
  });

  it("diffs a deleted file against an empty working side", async () => {
    await goTo(1, "old.py");
    const tab = vscode.window.tabGroups.activeTabGroup.activeTab;
    assert.ok(tab?.input instanceof vscode.TabInputTextDiff);
    assert.equal(tab.input.modified.scheme, "pear-empty");
  });

  it("diffs a new file against an empty HEAD side", async () => {
    await goTo(4, "new_module.py");
    const tab = vscode.window.tabGroups.activeTabGroup.activeTab;
    assert.ok(tab?.input instanceof vscode.TabInputTextDiff);
    assert.equal(tab.input.original.scheme, "pear-empty");
  });

  it("moves with Next and Prev", async () => {
    await goTo(0, "calc.py");
    await vscode.commands.executeCommand("pearReview.next");
    await waitFor("Next to reach old.py", async () => (await currentHunk())?.file === "old.py");
    await vscode.commands.executeCommand("pearReview.prev");
    await waitFor("Prev to return to calc.py", async () => (await currentHunk())?.file === "calc.py");
  });

  it("marks the current hunk reviewed, and back", async () => {
    await goTo(0, "calc.py");
    await vscode.commands.executeCommand("pearReview.toggleReviewed");
    await waitFor("calc.py's hunk to show as reviewed", async () => {
      const calc = (await tree()).files.find((f) => f.label === "calc.py");
      return calc?.hunks?.[0]?.icon === "pass-filled";
    });
    assert.match((await tree()).description ?? "", /1\/5 reviewed/);
    await vscode.commands.executeCommand("pearReview.toggleReviewed");
    await waitFor("the mark to clear", async () => /0\/5 reviewed/.test((await tree()).description ?? ""));
  });
});
