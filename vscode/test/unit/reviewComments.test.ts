import assert from "node:assert/strict";
import { test } from "node:test";

import type { CommentAnchor, ReviewComment } from "../../src/backend/protocol.ts";
import { patchHunks, toGithubComment } from "../../src/github/reviewComments.ts";

const anchor = (fo: number | null, fn: number | null, lo: number | null, ln: number | null): CommentAnchor => ({
  first_old_lineno: fo,
  first_new_lineno: fn,
  last_old_lineno: lo,
  last_new_lineno: ln,
});

const comment = (a: CommentAnchor | null, overrides: Partial<ReviewComment> = {}): ReviewComment => ({
  id: 1,
  file_path: "calc.py",
  where: "calc.py",
  instruction: "Handle a zero divisor.",
  severity: "must-fix",
  anchor: a,
  ...overrides,
});

// Two hunks: old 1–4 / new 1–5, and old 20–22 / new 21–23.
const PATCH = "@@ -1,4 +1,5 @@\n a\n-b\n+b2\n+b3\n c\n d\n@@ -20,3 +21,3 @@ def f():\n x\n-y\n+y2\n z";
const patches = new Map([["calc.py", PATCH]]);

void test("a patch's hunks cover their lines on each side", () => {
  assert.deepEqual(patchHunks(PATCH), [
    { left: [1, 4], right: [1, 5] },
    { left: [20, 22], right: [21, 23] },
  ]);
  assert.deepEqual(patchHunks(undefined), []);
});

void test("a new-side line inside a hunk is an inline RIGHT comment, severity first", () => {
  assert.deepEqual(toGithubComment(comment(anchor(null, 3, null, 3)), patches), {
    path: "calc.py",
    body: "**Must fix:** Handle a zero divisor.",
    line: 3,
    side: "RIGHT",
  });
});

void test("a range inside one hunk keeps its start line", () => {
  assert.deepEqual(toGithubComment(comment(anchor(null, 21, null, 23), { severity: "nit" }), patches), {
    path: "calc.py",
    body: "**Nit:** Handle a zero divisor.",
    line: 23,
    side: "RIGHT",
    start_line: 21,
    start_side: "RIGHT",
  });
});

void test("removed lines are a LEFT comment", () => {
  const result = toGithubComment(comment(anchor(2, null, 2, null)), patches);
  assert.equal(result.side, "LEFT");
  assert.equal(result.line, 2);
});

void test("lines outside the hunks, or across two, fall back to a file comment naming them", () => {
  assert.deepEqual(toGithubComment(comment(anchor(null, 10, null, 12), { severity: "suggestion" }), patches), {
    path: "calc.py",
    body: "Lines 10–12: **Suggestion:** Handle a zero divisor.",
    subject_type: "file",
  });
  assert.equal(toGithubComment(comment(anchor(null, 4, null, 22)), patches).subject_type, "file");
  assert.equal(
    toGithubComment(comment(anchor(30, null, 30, null)), patches).body.startsWith("Line 30 (before): "),
    true,
  );
});

void test("a file GitHub sent no patch for, or a comment with no anchor, is file-level", () => {
  assert.equal(
    toGithubComment(comment(anchor(null, 3, null, 3), { file_path: "big.json" }), patches).subject_type,
    "file",
  );
  assert.deepEqual(toGithubComment(comment(null), patches), {
    path: "calc.py",
    body: "**Must fix:** Handle a zero divisor.",
    subject_type: "file",
  });
});
