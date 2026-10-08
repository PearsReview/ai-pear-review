import assert from "node:assert/strict";
import { test } from "node:test";

import type { DiffLine } from "../../src/backend/protocol.ts";
import { MAX_MARKED_LINES, markedLines, selectionLabel } from "../../src/review/markedLines.ts";

const full: DiffLine[] = [
  { kind: "context", old_lineno: 1, new_lineno: 1, text: "def add(a, b):" },
  { kind: "add", old_lineno: null, new_lineno: 2, text: '    """Add."""' },
  { kind: "del", old_lineno: 2, new_lineno: null, text: "    return a+b" },
  { kind: "add", old_lineno: null, new_lineno: 3, text: "    return a + b" },
];
const text = (n: number): string => `line ${n}`;

void test("a working-file selection marks added and context lines from the diff", () => {
  const lines = markedLines({ filePath: "calc.py", side: "new", startLine: 1, endLine: 3 }, text, full);
  assert.deepEqual(
    lines.map((l) => [l.kind, l.old_lineno, l.new_lineno, l.text]),
    [
      ["context", 1, 1, "line 1"],
      ["add", null, 2, "line 2"],
      ["add", null, 3, "line 3"],
    ],
  );
});

void test("a HEAD-side selection marks removed lines", () => {
  const lines = markedLines({ filePath: "calc.py", side: "old", startLine: 2, endLine: 2 }, text, full);
  assert.deepEqual(lines, [{ file_path: "calc.py", old_lineno: 2, new_lineno: null, text: "line 2", kind: "del" }]);
});

void test("without the file's diff every line is context on its own side", () => {
  const lines = markedLines({ filePath: "other.py", side: "new", startLine: 5, endLine: 6 }, text);
  assert.deepEqual(
    lines.map((l) => [l.kind, l.old_lineno, l.new_lineno]),
    [
      ["context", null, 5],
      ["context", null, 6],
    ],
  );
});

void test("long selections are capped, and the label says so", () => {
  const sel = { filePath: "src/big.py", side: "new" as const, startLine: 1, endLine: 500 };
  assert.equal(markedLines(sel, text).length, MAX_MARKED_LINES);
  assert.equal(selectionLabel(sel), "big.py, lines 1–200 (first 200)");
  assert.equal(selectionLabel({ filePath: "a.py", side: "old", startLine: 4, endLine: 4 }), "a.py, line 4 (HEAD)");
});
