// protocol.ts against backend/docs/wire-protocol.md, both directions.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import { CLIENT_MESSAGE_TYPES, SERVER_MESSAGE_TYPES, isServerMessage } from "../../src/backend/protocol.ts";

const doc = readFileSync(new URL("../../backend/docs/wire-protocol.md", import.meta.url), "utf8");

// The first backticked cell of each row between two headings — the same parse the
// backend's own test_wire_protocol_doc_matches_the_registry uses.
function documented(from: string, to: string): string[] {
  const section = doc.split(from)[1]?.split(to)[0];
  assert.ok(section, `wire-protocol.md has no "${from}" section`);
  return [...section.matchAll(/^\| `([a-z_]+)` \|/gm)].map((m) => m[1] as string);
}

void test("every client → server message in wire-protocol.md is typed, and nothing else", () => {
  const doc = documented("## Browser → Python", "## Python → Browser");
  assert.deepEqual([...CLIENT_MESSAGE_TYPES].sort(), [...doc].sort());
});

void test("every server → client message in wire-protocol.md is typed, and nothing else", () => {
  const doc = documented("## Python → Browser", "## Fields shared across messages");
  assert.deepEqual([...SERVER_MESSAGE_TYPES].sort(), [...doc].sort());
});

void test("isServerMessage accepts known messages and rejects the rest", () => {
  assert.ok(isServerMessage({ type: "narration", payload: { text: "hi" } }));
  assert.ok(!isServerMessage({ type: "no_such_message", payload: {} }));
  assert.ok(!isServerMessage({ type: "narration" }));
  assert.ok(!isServerMessage("narration"));
});
