import assert from "node:assert/strict";
import { test } from "node:test";

import { blockAtFraction } from "../../src/review/reading.ts";

const blocks = [
  { block_index: 0, start_line: 1, end_line: 1, weight: 1 },
  { block_index: 1, start_line: 3, end_line: 4, weight: 3 },
];

void test("the voice's block follows the clip's progress by spoken weight", () => {
  assert.equal(blockAtFraction(blocks, 0)?.block_index, 0);
  assert.equal(blockAtFraction(blocks, 0.2)?.block_index, 0);
  assert.equal(blockAtFraction(blocks, 0.3)?.block_index, 1);
  assert.equal(blockAtFraction(blocks, 1)?.block_index, 1);
});

void test("no blocks, or no weights, still answer sensibly", () => {
  assert.equal(blockAtFraction([], 0.5), undefined);
  assert.equal(blockAtFraction([{ block_index: 7, start_line: 1, end_line: 1, weight: 0 }], 0.9)?.block_index, 7);
});
