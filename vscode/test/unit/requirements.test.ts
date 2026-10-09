import assert from "node:assert/strict";
import { test } from "node:test";

import { requirementsStamp } from "../../src/backend/requirements.ts";

const FILE = "fastapi>=0.110\nopenai>=1.0,<3.0\n";

void test("a release that changes requirements.txt changes the stamp", () => {
  assert.notEqual(requirementsStamp(FILE), requirementsStamp(FILE.replace("openai>=1.0,<3.0\n", "")));
});

void test("the same file checked out with Windows line endings is not a change", () => {
  assert.equal(requirementsStamp(FILE), requirementsStamp(FILE.replace(/\n/g, "\r\n")));
  assert.equal(requirementsStamp(FILE), requirementsStamp(`${FILE}\n\n`));
});
