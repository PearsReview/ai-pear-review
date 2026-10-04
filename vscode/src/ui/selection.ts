import * as vscode from "vscode";

import type { Backend } from "../backend/backend.ts";
import type { DiffLine, MarkedLine } from "../backend/protocol.ts";
import { reviewLocation } from "../git.ts";
import { markedLines, selectionLabel, type Selection } from "../review/markedLines.ts";

export interface SelectionContext {
  // "calc.py, lines 2–4", or undefined when nothing usable is selected.
  readonly label: string | undefined;
  readonly onDidChange: vscode.Event<string | undefined>;
  // The selection as marked_lines, for the next reply.
  markedLines(): MarkedLine[] | undefined;
  // Any range of a review document as marked_lines (a comment thread's lines).
  markedLinesAt(sel: Selection, document: vscode.TextDocument): MarkedLine[];
  clear(): void;
}

// The editor selection is the question's context, the way double-click markers are in
// the browser. Either side of the review diff counts, and so does any file in the repo.
export function register(backend: Backend): { selection: SelectionContext; disposables: vscode.Disposable[] } {
  const changes = new vscode.EventEmitter<string | undefined>();
  let presented: { filePath: string; fullLines: DiffLine[] } | undefined;
  let current: { sel: Selection; editor: vscode.TextEditor } | undefined;

  const update = (): void => {
    const editor = vscode.window.activeTextEditor;
    const sel = editor && backend.repoPath ? toSelection(editor, backend.repoPath) : undefined;
    const before = current && selectionLabel(current.sel);
    current = sel && editor ? { sel, editor } : undefined;
    const after = current && selectionLabel(current.sel);
    if (before !== after) changes.fire(after);
  };

  const selection: SelectionContext = {
    get label() {
      return current && selectionLabel(current.sel);
    },
    onDidChange: changes.event,
    markedLines() {
      return current && selection.markedLinesAt(current.sel, current.editor.document);
    },
    markedLinesAt(sel, document) {
      // Only the file on screen has its diff here; any other file's lines are context.
      const fullLines = presented?.filePath === sel.filePath ? presented.fullLines : undefined;
      const lineText = (n: number): string => (n - 1 < document.lineCount ? document.lineAt(n - 1).text : "");
      return markedLines(sel, lineText, fullLines);
    },
    clear() {
      if (!current) return;
      const { editor } = current;
      editor.selection = new vscode.Selection(editor.selection.active, editor.selection.active);
      update();
    },
  };

  return {
    selection,
    disposables: [
      changes,
      vscode.window.onDidChangeTextEditorSelection(update),
      vscode.window.onDidChangeActiveTextEditor(update),
      backend.on("presenting", (p) => {
        presented = p.file_path ? { filePath: p.file_path, fullLines: p.full_lines ?? [] } : undefined;
      }),
    ],
  };
}

function toSelection(editor: vscode.TextEditor, repoRoot: string): Selection | undefined {
  const { selection, document } = editor;
  if (selection.isEmpty) return undefined;
  const location = reviewLocation(document.uri, repoRoot);
  if (!location) return undefined;
  return { ...location, ...lineSpan(selection) };
}

// 1-based inclusive lines of a range. One ending at column 0 doesn't include that line.
export function lineSpan(range: vscode.Range): { startLine: number; endLine: number } {
  const endLine = range.end.character === 0 && range.end.line > range.start.line ? range.end.line : range.end.line + 1;
  return { startLine: range.start.line + 1, endLine };
}
