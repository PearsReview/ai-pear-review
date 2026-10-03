// Runs inside the VS Code test instance (see runTest.ts): loads every bundled
// *.test.js next to this file, in name order, so the numbered files run as one story.
import { readdirSync } from "node:fs";
import * as path from "node:path";

import Mocha from "mocha";

export function run(): Promise<void> {
  const live = process.env.PEAR_TEST_LIVE_MODEL === "1";
  // A real local model takes tens of seconds per reply.
  const mocha = new Mocha({ ui: "bdd", color: true, timeout: live ? 600_000 : 60_000, bail: false });
  for (const file of readdirSync(__dirname)
    .filter((f) => f.endsWith(".test.js"))
    .sort()) {
    mocha.addFile(path.join(__dirname, file));
  }
  return new Promise((resolve, reject) => {
    mocha.run((failures) => (failures ? reject(new Error(`${failures} integration test(s) failed`)) : resolve()));
  });
}
