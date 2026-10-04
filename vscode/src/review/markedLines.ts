// An editor selection → the backend's marked_lines (app/web/context.py). Pure, so it's
// unit-tested (test/unit/markedLines.test.ts).
import type { DiffLine, MarkedLine } from "../backend/protocol.ts";

// A selection this long is a whole file, not a question's context. The model gets the
// first lines, and the label says so.
export const MAX_MARKED_LINES = 200;

export interface Selection {
  filePath: string;
  // "new" is the working file, "old" is HEAD (the diff's left side).
  side: "new" | "old";
  // 1-based, inclusive.
  startLine: number;
  endLine: number;
}

// `fullLines` is the file's whole diff when the selected file is the one on screen;
// with it, each line is marked as added/removed/context. Without it (another file),
// every line is plain context.
export function markedLines(sel: Selection, lineText: (line: number) => string, fullLines?: DiffLine[]): MarkedLine[] {
  const end = Math.min(sel.endLine, sel.startLine + MAX_MARKED_LINES - 1);
  const byLine = new Map<number, DiffLine>();
  for (const line of fullLines ?? []) {
    const n = sel.side === "new" ? line.new_lineno : line.old_lineno;
    if (n !== null) byLine.set(n, line);
  }
  const result: MarkedLine[] = [];
  for (let n = sel.startLine; n <= end; n++) {
    const diff = byLine.get(n);
    result.push({
      file_path: sel.filePath,
      old_lineno: diff ? diff.old_lineno : sel.side === "old" ? n : null,
      new_lineno: diff ? diff.new_lineno : sel.side === "new" ? n : null,
      text: lineText(n),
      kind: diff?.kind ?? "context",
    });
  }
  return result;
}

// What the chat shows above the composer: "calc.py, lines 2–4 (HEAD)".
export function selectionLabel(sel: Selection): string {
  const name = sel.filePath.split("/").pop() ?? sel.filePath;
  const truncated = sel.endLine - sel.startLine + 1 > MAX_MARKED_LINES;
  const end = truncated ? sel.startLine + MAX_MARKED_LINES - 1 : sel.endLine;
  const where = sel.startLine === end ? `line ${sel.startLine}` : `lines ${sel.startLine}–${end}`;
  return `${name}, ${where}${sel.side === "old" ? " (HEAD)" : ""}${truncated ? " (first 200)" : ""}`;
}
