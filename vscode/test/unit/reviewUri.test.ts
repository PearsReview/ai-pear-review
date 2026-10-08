import assert from "node:assert/strict";
import { test } from "node:test";

import { parseReviewQuery } from "../../src/review/reviewUri.ts";

void test("parseReviewQuery reads the file and side of a GitHub PR diff document", () => {
  const base = JSON.stringify({ path: "/repo/calc.py", ref: "main", commit: "abc", base: true, rootPath: "/repo" });
  assert.deepEqual(parseReviewQuery(base, "/x"), { path: "/repo/calc.py", side: "old" });
  const head = JSON.stringify({ path: "/repo/calc.py", commit: "def", base: false });
  assert.deepEqual(parseReviewQuery(head, "/x"), { path: "/repo/calc.py", side: "new" });
});

void test("parseReviewQuery falls back to the URI's own path, and rejects what isn't its JSON", () => {
  assert.deepEqual(parseReviewQuery(JSON.stringify({ commit: "abc" }), "/repo/mul.py"), {
    path: "/repo/mul.py",
    side: "new",
  });
  assert.equal(parseReviewQuery("not json", "/repo/a.py"), undefined);
  assert.equal(parseReviewQuery("null", "/repo/a.py"), undefined);
  assert.equal(parseReviewQuery(JSON.stringify({}), ""), undefined);
});
