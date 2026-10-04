import assert from "node:assert/strict";
import { test } from "node:test";

import { sourceLinesOf } from "../../src/review/selectionLines.ts";

const SOURCE = [
  "# Demo notes", // 1
  "", // 2
  "This repo holds a **tiny** calculator.", // 3
  "", // 4
  "## Usage", // 5
  "", // 6
  "- Call `add` with two numbers.", // 7
  "- Read the [result](https://example.com/docs/result).", // 8
  "",
].join("\n");

void test("a selection inside one paragraph maps to its line, markdown syntax and all", () => {
  assert.deepEqual(sourceLinesOf(SOURCE, "holds a tiny calculator"), { startLine: 3, endLine: 3 });
});

void test("a selection across blocks maps to the lines it spans", () => {
  // As copied from the preview: rendered text, blocks on their own lines.
  assert.deepEqual(sourceLinesOf(SOURCE, "Usage\nCall add with two numbers."), { startLine: 5, endLine: 7 });
});

void test("a link's address between the words still finds the selection's start and end", () => {
  assert.deepEqual(sourceLinesOf(SOURCE, "Call add with two numbers.\nRead the result."), { startLine: 7, endLine: 8 });
});

void test("Windows line endings, and text that isn't in the file", () => {
  assert.deepEqual(sourceLinesOf(SOURCE.replace(/\n/g, "\r\n"), "Usage"), { startLine: 5, endLine: 5 });
  assert.equal(sourceLinesOf(SOURCE, "Nothing like this appears"), undefined);
  assert.equal(sourceLinesOf(SOURCE, "  …  "), undefined);
});
