import assert from "node:assert/strict";
import { test } from "node:test";

import type { CommentAnchor } from "../../src/backend/protocol.ts";
import { anchorRange } from "../../src/review/anchors.ts";

const anchor = (fo: number | null, fn: number | null, lo: number | null, ln: number | null): CommentAnchor => ({
  first_old_lineno: fo,
  first_new_lineno: fn,
  last_old_lineno: lo,
  last_new_lineno: ln,
});

void test("a working-file selection anchors on the working file", () => {
  assert.deepEqual(anchorRange(anchor(1, 1, null, 3)), { side: "new", startLine: 1, endLine: 3 });
});

void test("a HEAD selection starting on context anchors on HEAD", () => {
  assert.deepEqual(anchorRange(anchor(1, 1, 2, null)), { side: "old", startLine: 1, endLine: 2 });
});

void test("a selection of removed lines only anchors on HEAD", () => {
  assert.deepEqual(anchorRange(anchor(61, null, 63, null)), { side: "old", startLine: 61, endLine: 63 });
});

void test("a whole-hunk comment, or one spanning both sides, falls back to the working file", () => {
  assert.deepEqual(anchorRange(null), { side: "new", startLine: 1, endLine: 1 });
  assert.deepEqual(anchorRange(anchor(5, null, null, 7)), { side: "new", startLine: 7, endLine: 7 });
});
