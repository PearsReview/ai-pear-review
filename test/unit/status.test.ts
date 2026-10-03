import assert from "node:assert/strict";
import { test } from "node:test";

import { downNames, statusRows, tokenLine } from "../../src/review/status.ts";

void test("a service that hasn't reported is unknown, not down", () => {
  const rows = statusRows({ llm: true });
  assert.deepEqual(
    rows.map((r) => [r.name, r.state]),
    [
      ["Reviewer model", "up"],
      ["Speech-to-text", "unknown"],
      ["Text-to-speech", "unknown"],
      ["Coding agent", "unknown"],
    ],
  );
  assert.deepEqual(downNames(rows), []);
});

void test("down services are named; an unchosen agent is off, not down", () => {
  const rows = statusRows({ llm: true, stt: false, tts: false, act_now: { available: false, detail: "choose Cline" } });
  assert.deepEqual(downNames(rows), ["speech-to-text", "text-to-speech"]);
  assert.deepEqual(rows[3], { name: "Coding agent", state: "off", note: "choose Cline" });
});

void test("token use reads in thousands once it's large", () => {
  assert.equal(tokenLine({}), undefined);
  assert.equal(tokenLine({ llm_input_tokens: 812, llm_output_tokens: 95 }), "812 tokens in · 95 out this session");
  assert.equal(
    tokenLine({ llm_input_tokens: 48_200, llm_output_tokens: 3_100 }),
    "48k tokens in · 3,100 out this session",
  );
});
