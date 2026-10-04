import assert from "node:assert/strict";
import { test } from "node:test";

import type { DiffLine } from "../../src/backend/protocol.ts";
import { fileChange, hunkHighlight, hunkLabel, parseHeader, sidesOf } from "../../src/review/hunks.ts";

const ctx = (o: number, n: number): DiffLine => ({ kind: "context", old_lineno: o, new_lineno: n, text: "" });
const add = (n: number): DiffLine => ({ kind: "add", old_lineno: null, new_lineno: n, text: "" });
const del = (o: number): DiffLine => ({ kind: "del", old_lineno: o, new_lineno: null, text: "" });

void test("parseHeader reads counts, defaulting an omitted one to 1", () => {
  assert.deepEqual(parseHeader("@@ -10,4 +12,6 @@ def f():"), { oldStart: 10, oldCount: 4, newStart: 12, newCount: 6 });
  assert.deepEqual(parseHeader("@@ -3 +3 @@"), { oldStart: 3, oldCount: 1, newStart: 3, newCount: 1 });
  assert.equal(parseHeader("not a header"), undefined);
});

void test("fileChange tells added and deleted files from modified ones", () => {
  assert.equal(fileChange("@@ -0,0 +1,5 @@"), "added");
  assert.equal(fileChange("@@ -1,5 +0,0 @@"), "deleted");
  assert.equal(fileChange("@@ -1,5 +1,6 @@"), "modified");
});

void test("hunkLabel names the working-file lines", () => {
  assert.equal(hunkLabel("@@ -10,4 +12,6 @@"), "Lines 12–17");
  assert.equal(hunkLabel("@@ -3 +3 @@"), "Line 3");
  assert.equal(hunkLabel("@@ -8,2 +7,0 @@"), "Removed after line 7");
  assert.equal(hunkLabel("@@ -1,5 +0,0 @@"), "File deleted");
});

void test("hunkHighlight picks the hunk's added lines in the working file", () => {
  // Two hunks in one file; the second (indices 4..5) adds line 10 and changes line 11.
  const full = [ctx(1, 1), add(2), ctx(2, 3), ctx(8, 9), add(10), del(9), add(11), ctx(10, 12)];
  assert.deepEqual(hunkHighlight(full, 1, 1), { side: "new", lines: [2] });
  assert.deepEqual(hunkHighlight(full, 4, 6), { side: "new", lines: [10, 11] });
});

void test("a removal-only hunk is highlighted in HEAD", () => {
  const full = [ctx(1, 1), del(2), del(3), ctx(4, 2)];
  assert.deepEqual(hunkHighlight(full, 1, 2), { side: "old", lines: [2, 3] });
  assert.equal(hunkHighlight(full, 0, 0), undefined);
});

void test("sidesOf splits a whole-file diff into its before and after text", () => {
  const full: DiffLine[] = [
    { kind: "context", old_lineno: 1, new_lineno: 1, text: "a" },
    { kind: "del", old_lineno: 2, new_lineno: null, text: "b" },
    { kind: "add", old_lineno: null, new_lineno: 2, text: "B" },
    { kind: "context", old_lineno: 3, new_lineno: 3, text: "c" },
  ];
  assert.deepEqual(sidesOf(full), { before: "a\nb\nc", after: "a\nB\nc" });
  assert.deepEqual(sidesOf([]), { before: "", after: "" });
});
