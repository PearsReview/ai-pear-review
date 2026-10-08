import * as path from "node:path";
import * as vscode from "vscode";

import type { Backend } from "../backend/backend.ts";
import type { Presenting } from "../backend/protocol.ts";
import { prReviewFor } from "../github/prReviews.ts";
import { baseRef, gitApi } from "../git.ts";
import { log } from "../log.ts";
import { fileChange, hunkHighlight } from "../review/hunks.ts";
import { publish } from "../testProbe.ts";

// The side of a diff with no file behind it: HEAD for a new file, the working tree for
// a deleted one.
const EMPTY_SCHEME = "pear-empty";
// vscode.diff resolves before its editors are visible; how long to wait for them.
const EDITOR_WAIT_MS = 2_000;

interface Highlight {
  uri: string;
  ranges: vscode.Range[];
}

// Each presented hunk opens as a native diff (HEAD ↔ working file, or a pull request's
// merge-base ↔ its head), with the hunk's lines marked and scrolled into view.
export function register(backend: Backend): vscode.Disposable[] {
  const decoration = vscode.window.createTextEditorDecorationType({
    isWholeLine: true,
    backgroundColor: new vscode.ThemeColor("editor.rangeHighlightBackground"),
    borderStyle: "solid",
    borderColor: new vscode.ThemeColor("focusBorder"),
    borderWidth: "0 0 0 3px",
    overviewRulerColor: new vscode.ThemeColor("focusBorder"),
    overviewRulerLane: vscode.OverviewRulerLane.Full,
  });
  let current: Highlight | undefined;
  let generation = 0;
  publish("diff.highlight", () => current && { uri: current.uri, lines: current.ranges.map((r) => r.start.line + 1) });

  const apply = (editor: vscode.TextEditor): void => {
    if (current && editor.document.uri.toString() === current.uri) editor.setDecorations(decoration, current.ranges);
  };

  const show = async (p: Presenting): Promise<void> => {
    const mine = ++generation;
    const root = backend.repoPath;
    if (!root || !p.file_path || !p.header) return;
    const git = await gitApi();
    const fileUri = vscode.Uri.file(path.join(root, p.file_path));
    const empty = vscode.Uri.from({ scheme: EMPTY_SCHEME, path: `/${p.file_path}` });
    const change = fileChange(p.header);
    const left = change === "added" || !git ? empty : git.toGitUri(fileUri, baseRef(root));
    const right = change === "deleted" ? empty : fileUri;
    const pr = prReviewFor(root);
    const sides = pr ? `PR #${pr.number}` : "HEAD ↔ Working Tree";
    const title = `${path.posix.basename(p.file_path)} (${sides}) — change ${p.index + 1} of ${p.total}`;

    for (const editor of vscode.window.visibleTextEditors) editor.setDecorations(decoration, []);
    const highlight = hunkHighlight(p.full_lines ?? [], p.highlight_start ?? -1, p.highlight_end ?? -1);
    const target = highlight?.side === "old" ? left : right;
    current = highlight && {
      uri: target.toString(),
      ranges: highlight.lines.map((n) => new vscode.Range(n - 1, 0, n - 1, 0)),
    };

    await vscode.commands.executeCommand("vscode.diff", left, right, title, { preview: true, preserveFocus: true });
    const editor = await waitForEditor(target.toString(), () => mine !== generation);
    if (!editor || mine !== generation || !current) return;
    apply(editor);
    const first = current.ranges[0];
    const last = current.ranges[current.ranges.length - 1];
    if (first && last) editor.revealRange(first.union(last), vscode.TextEditorRevealType.InCenterIfOutsideViewport);
  };

  return [
    decoration,
    vscode.workspace.registerTextDocumentContentProvider(EMPTY_SCHEME, { provideTextDocumentContent: () => "" }),
    backend.on("presenting", (p) => {
      if (p.done) return;
      show(p).catch((err: unknown) => log(`Couldn't open the diff: ${String(err)}`));
    }),
    // Decorations belong to an editor instance; switching back to the tab makes a new one.
    vscode.window.onDidChangeVisibleTextEditors((editors) => editors.forEach(apply)),
  ];
}

async function waitForEditor(uri: string, cancelled: () => boolean): Promise<vscode.TextEditor | undefined> {
  const deadline = Date.now() + EDITOR_WAIT_MS;
  while (Date.now() < deadline && !cancelled()) {
    const editor = vscode.window.visibleTextEditors.find((e) => e.document.uri.toString() === uri);
    if (editor) return editor;
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  return undefined;
}
