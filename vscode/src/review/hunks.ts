// Pure helpers over the backend's hunk data: no vscode import, so they're unit-tested
// directly (test/unit/hunks.test.ts).
import type { DiffLine } from "../backend/protocol.ts";

const HEADER = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@/;

export interface HeaderRange {
  oldStart: number;
  oldCount: number;
  newStart: number;
  newCount: number;
}

export function parseHeader(header: string): HeaderRange | undefined {
  const m = HEADER.exec(header);
  if (!m) return undefined;
  // A count git omits is 1 ("@@ -3 +3 @@").
  return {
    oldStart: Number(m[1]),
    oldCount: m[2] === undefined ? 1 : Number(m[2]),
    newStart: Number(m[3]),
    newCount: m[4] === undefined ? 1 : Number(m[4]),
  };
}

// "-0,0": nothing in HEAD (a new or untracked file). "+0,0": nothing on disk (deleted).
export function fileChange(header: string): "added" | "deleted" | "modified" {
  const range = parseHeader(header);
  if (range?.oldStart === 0 && range.oldCount === 0) return "added";
  if (range?.newStart === 0 && range.newCount === 0) return "deleted";
  return "modified";
}

// The tree label for a hunk: where it sits in the working file.
export function hunkLabel(header: string): string {
  const range = parseHeader(header);
  if (!range) return header;
  if (range.newCount === 0) return range.newStart === 0 ? "File deleted" : `Removed after line ${range.newStart}`;
  const end = range.newStart + range.newCount - 1;
  return end === range.newStart ? `Line ${range.newStart}` : `Lines ${range.newStart}–${end}`;
}

export interface HunkHighlight {
  // Which side of the diff editor the lines are on: the working file, or HEAD when the
  // hunk only removes lines.
  side: "new" | "old";
  // 1-based line numbers on that side.
  lines: number[];
}

// The lines to highlight for the hunk at full[start..end] (inclusive): its added lines
// in the working file, or, for a removal-only hunk, its removed lines in HEAD.
export function hunkHighlight(full: DiffLine[], start: number, end: number): HunkHighlight | undefined {
  const changed = full.slice(Math.max(start, 0), end + 1);
  const added = changed.flatMap((l) => (l.kind === "add" && l.new_lineno !== null ? [l.new_lineno] : []));
  if (added.length) return { side: "new", lines: added };
  const removed = changed.flatMap((l) => (l.kind === "del" && l.old_lineno !== null ? [l.old_lineno] : []));
  if (removed.length) return { side: "old", lines: removed };
  return undefined;
}

// A whole-file diff (an Act Now preview's full_lines) back into the text on each side.
// Context lines are on both; removed lines only before, added lines only after.
export function sidesOf(full: DiffLine[]): { before: string; after: string } {
  const before = full.filter((l) => l.kind !== "add").map((l) => l.text);
  const after = full.filter((l) => l.kind !== "del").map((l) => l.text);
  return { before: before.join("\n"), after: after.join("\n") };
}

// The hunk of a file a line is in, on one side of its diff: for "Explain this change"
// from wherever the cursor is (Pear's diff, GitHub's, or the plain file). A line between
// hunks picks the nearest one, so the button never does nothing on a reviewed file.
export function hunkAtLine(
  hunks: readonly { index: number; header: string }[],
  side: "new" | "old",
  line: number,
): number | undefined {
  let nearest: { index: number; distance: number } | undefined;
  for (const hunk of hunks) {
    const range = parseHeader(hunk.header);
    if (!range) continue;
    const start = side === "new" ? range.newStart : range.oldStart;
    const count = side === "new" ? range.newCount : range.oldCount;
    const end = start + Math.max(count, 1) - 1;
    const distance = line < start ? start - line : line > end ? line - end : 0;
    if (distance === 0) return hunk.index;
    if (!nearest || distance < nearest.distance) nearest = { index: hunk.index, distance };
  }
  return nearest?.index;
}
