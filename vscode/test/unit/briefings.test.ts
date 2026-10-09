import assert from "node:assert/strict";
import { test } from "node:test";

import type { BriefingStatus } from "../../src/backend/protocol.ts";
import { briefingButtons, hunkBriefing, hunkBriefingLabel, warningKey } from "../../src/review/briefings.ts";

const status = (over: Partial<BriefingStatus> = {}): BriefingStatus => ({
  total: 4,
  out_of_date: [1, 2, 3],
  changed: 1,
  stale_context: [],
  latest_change: null,
  latest_briefing: null,
  message: "3 of 4 changes have no up-to-date briefing.",
  ...over,
});

void test("a change's briefing state, and the row's word for it", () => {
  assert.deepEqual(
    [0, 1, 2, 3].map((i) => hunkBriefing(status(), i)),
    ["current", "out_of_date", "out_of_date", "out_of_date"],
  );
  assert.equal(hunkBriefing(undefined, 1), "current");
  assert.equal(hunkBriefingLabel("current"), undefined);
  assert.equal(hunkBriefingLabel("out_of_date"), "not briefed");
});

void test("the same state gives the same warning key, a different one doesn't", () => {
  assert.equal(warningKey(status()), warningKey(status({ changed: 2 })));
  assert.notEqual(warningKey(status()), warningKey(status({ out_of_date: [1] })));
  assert.notEqual(warningKey(status()), warningKey(status({ stale_context: ["project overview"] })));
});

void test("the warning offers each installed assistant, or a copy without one", () => {
  assert.deepEqual(
    briefingButtons(() => true),
    ["Brief in Claude Code", "Brief in Cline"],
  );
  assert.deepEqual(
    briefingButtons((id) => id === "saoudrizwan.claude-dev"),
    ["Brief in Cline"],
  );
  assert.deepEqual(
    briefingButtons(() => false),
    ["Copy Instruction"],
  );
});
