import * as path from "node:path";
import * as vscode from "vscode";

import type { Backend } from "../backend/backend.ts";
import type { DiffLine, MarkedLine } from "../backend/protocol.ts";
import { markedLines, selectionLabel, type Selection } from "../review/markedLines.ts";

export interface SelectionContext {
  // "calc.py, lines 2–4", or undefined when nothing usable is selected.
  readonly label: string | undefined;
  readonly onDidChange: vscode.Event<string | undefined>;
  // The selection as marked_lines, for the next reply.
  markedLines(): MarkedLine[] | undefined;
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
      if (!current) return undefined;
      const { sel, editor } = current;
      const fullLines = presented?.filePath === sel.filePath ? presented.fullLines : undefined;
      return markedLines(sel, (n) => editor.document.lineAt(n - 1).text, fullLines);
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
  // The git extension's HEAD side (toGitUri) keeps the file's own path.
  const side = document.uri.scheme === "file" ? "new" : document.uri.scheme === "git" ? "old" : undefined;
  if (!side) return undefined;
  const relative = path.relative(repoRoot, document.uri.fsPath);
  if (!relative || relative.startsWith("..") || path.isAbsolute(relative)) return undefined;
  // A selection ending at column 0 of a line doesn't include that line.
  const endLine =
    selection.end.character === 0 && selection.end.line > selection.start.line
      ? selection.end.line
      : selection.end.line + 1;
  return { filePath: relative.split(path.sep).join("/"), side, startLine: selection.start.line + 1, endLine };
}
