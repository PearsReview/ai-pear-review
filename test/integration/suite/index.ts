// Runs inside the VS Code test instance (see runTest.ts): loads the bundled *.test.js
// files of this run's workspace, in name order, so they run as one story. The main run
// (one repo) takes the numbered files; the others take files named for them
// (multi_*: a multi-root workspace, nogit_*: a folder that isn't a repository).
import { readdirSync } from "node:fs";
import * as path from "node:path";

import Mocha from "mocha";

export function run(): Promise<void> {
  const live = process.env.PEAR_TEST_LIVE_MODEL === "1";
  // A real local model takes tens of seconds per reply.
  const mocha = new Mocha({ ui: "bdd", color: true, timeout: live ? 600_000 : 60_000, bail: false });
  const suite = process.env.PEAR_TEST_SUITE ?? "main";
  const ours = (f: string): boolean => (suite === "main" ? /^\d/.test(f) : f.startsWith(`${suite}_`));
  for (const file of readdirSync(__dirname)
    .filter((f) => f.endsWith(".test.js") && ours(f))
    .sort()) {
    mocha.addFile(path.join(__dirname, file));
  }
  return new Promise((resolve, reject) => {
    mocha.run((failures) => (failures ? reject(new Error(`${failures} integration test(s) failed`)) : resolve()));
  });
}
