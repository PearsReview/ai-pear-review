import * as path from "node:path";
import * as vscode from "vscode";

import type { Backend } from "../backend/backend.ts";
import { reviewLocation } from "../git.ts";
import { publish } from "../testProbe.ts";
import { previewShowing } from "./previewTabs.ts";

// Where a markdown file is being read aloud: the passage being read.
export interface ReadingPosition {
  filePath: string;
  // 1-based, inclusive.
  startLine: number;
  endLine: number;
}

export interface ReadingHighlight {
  readonly position: ReadingPosition | undefined;
  // Moves the highlight to a passage, or clears it.
  set(position: ReadingPosition | undefined): void;
}

// The passage being read aloud, marked in the file's text (a decoration, scrolled into
// view) and in its markdown preview (re-rendered, so markdownReading.ts marks it).
export function register(backend: Backend): { highlight: ReadingHighlight; disposables: vscode.Disposable[] } {
  const decoration = vscode.window.createTextEditorDecorationType({
    isWholeLine: true,
    backgroundColor: new vscode.ThemeColor("editor.findMatchBackground"),
    borderStyle: "solid",
    borderColor: new vscode.ThemeColor("editor.findMatchBorder"),
    borderWidth: "0 0 0 3px",
    overviewRulerColor: new vscode.ThemeColor("editor.findMatchBackground"),
    overviewRulerLane: vscode.OverviewRulerLane.Center,
  });
  let position: ReadingPosition | undefined;
  publish("files.reading", () => position);

  const decorate = (editor: vscode.TextEditor, reveal: boolean): void => {
    const root = backend.repoPath;
    const location = root ? reviewLocation(editor.document.uri, root) : undefined;
    if (!position || location?.side !== "new" || location.filePath !== position.filePath) {
      editor.setDecorations(decoration, []);
      return;
    }
    const range = new vscode.Range(position.startLine - 1, 0, position.endLine - 1, 0);
    editor.setDecorations(decoration, [range]);
    // Scrolls, but never moves the cursor or selection: the reviewer may be working in
    // the file while it's read. The preview follows on its own (markdownReading.ts).
    if (reveal) editor.revealRange(range, vscode.TextEditorRevealType.InCenterIfOutsideViewport);
  };

  // The highlight needs the file's text on screen. Read with the file closed (and no
  // preview of it showing), the text opens beside, once per read, without taking focus.
  let textShownFor: string | undefined;
  const ensureTextVisible = async (filePath: string): Promise<void> => {
    const root = backend.repoPath;
    if (!root || textShownFor === filePath) return;
    textShownFor = filePath;
    const shown = vscode.window.visibleTextEditors.some(
      (e) => e.document.uri.scheme === "file" && reviewLocation(e.document.uri, root)?.filePath === filePath,
    );
    if (shown || previewShowing(filePath)) return;
    await vscode.window.showTextDocument(vscode.Uri.file(path.join(root, filePath)), {
      viewColumn: vscode.ViewColumn.Beside,
      preserveFocus: true,
      preview: true,
    });
  };

  // The preview marks the passage as it renders, so it re-renders as the reading moves.
  let refreshQueued = false;
  const refreshPreview = (filePath: string | undefined): void => {
    if (!filePath || !previewShowing(filePath) || refreshQueued) return;
    refreshQueued = true;
    setTimeout(() => {
      refreshQueued = false;
      void vscode.commands.executeCommand("markdown.preview.refresh");
    }, 50);
  };

  const highlight: ReadingHighlight = {
    get position() {
      return position;
    },
    set(next) {
      const same =
        next &&
        position &&
        next.filePath === position.filePath &&
        next.startLine === position.startLine &&
        next.endLine === position.endLine;
      if (same) return;
      const was = position?.filePath;
      position = next;
      refreshPreview(next?.filePath ?? was);
      if (!next) {
        textShownFor = undefined;
        for (const editor of vscode.window.visibleTextEditors) decorate(editor, false);
        return;
      }
      void ensureTextVisible(next.filePath).then(() => {
        for (const editor of vscode.window.visibleTextEditors) decorate(editor, true);
      });
    },
  };

  return {
    highlight,
    disposables: [
      decoration,
      // Decorations belong to an editor instance; reopening the file makes a new one.
      vscode.window.onDidChangeVisibleTextEditors((editors) => editors.forEach((e) => decorate(e, false))),
    ],
  };
}
