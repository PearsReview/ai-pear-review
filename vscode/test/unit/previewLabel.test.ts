import assert from "node:assert/strict";
import { test } from "node:test";

import { labelNames } from "../../src/review/previewLabel.ts";

void test("a preview label names its file in any language", () => {
  assert.ok(labelNames("Preview README.md", "README.md"));
  assert.ok(labelNames("[Preview] README.md", "README.md"));
  assert.ok(labelNames("Vorschau README.md", "README.md"));
  assert.ok(labelNames("README.md", "README.md"));
});

void test("a name that only ends the same doesn't match", () => {
  assert.ok(!labelNames("Preview MY_README.md", "README.md"));
  assert.ok(!labelNames("Preview README.md", "OTHER.md"));
});
