// Runs the integration suite: a real VS Code with this extension, opened on a scratch
// repo, with a real backend behind it and fakes for everything that backend calls out to.
//
//   npm run test:integration          fake model (default)
//   npm run test:integration:ollama   your real local Ollama (or PEAR_TEST_MODEL=ollama)
//
// The coding agent and the speech service are always fakes; the microphone is a fake
// sounddevice that records a tone.
import { existsSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import * as path from "node:path";

import { runTests } from "@vscode/test-electron";

import { startFakeServices } from "./fakeServices.ts";
import { cleanup, prepareBackend, seedRepo } from "./fixtures.ts";

const ROOT = path.resolve(import.meta.dirname, "..", "..");
const liveModel = process.env.PEAR_TEST_MODEL === "ollama" || process.argv.includes("--ollama");

const venvPython =
  process.platform === "win32"
    ? path.join(ROOT, ".venv", "Scripts", "python.exe")
    : path.join(ROOT, ".venv", "bin", "python");
const python = process.env.PEAR_TEST_PYTHON ?? (existsSync(venvPython) ? venvPython : "python");

// Run from inside VS Code (its terminal, or an agent in an extension), this process
// inherits the parent's ELECTRON_RUN_AS_NODE, which makes the test instance's Code.exe
// behave as plain Node, and VSCODE_* handles that point at the running editor.
for (const name of Object.keys(process.env)) {
  if (name === "ELECTRON_RUN_AS_NODE" || name.startsWith("VSCODE_")) delete process.env[name];
}

const work = mkdtempSync(path.join(tmpdir(), "pear-it-"));
const repo = path.join(work, "repo");
const fake = await startFakeServices();
let failed = false;
try {
  seedRepo(repo);
  const backend = prepareBackend(work, repo, { fakeUrl: fake.url, liveModel, python });
  console.log(`Integration run: ${liveModel ? "real Ollama" : "fake model"}; scratch dir ${work}`);
  await runTests({
    extensionDevelopmentPath: ROOT,
    extensionTestsPath: path.join(ROOT, "dist-test", "suite", "index.js"),
    launchArgs: [
      repo,
      "--disable-extensions",
      "--disable-workspace-trust",
      "--skip-welcome",
      "--skip-release-notes",
      "--user-data-dir",
      path.join(work, "user-data"),
    ],
    extensionTestsEnv: {
      PEAR_REVIEW_TEST: "1",
      PEAR_REVIEW_BACKEND_DIR: backend,
      PEAR_TEST_FAKE_URL: fake.url,
      PEAR_TEST_LIVE_MODEL: liveModel ? "1" : "",
      PEAR_TEST_REPO: repo,
      PYTHONPATH: path.join(ROOT, "test", "integration", "fake_modules"),
      CLINE_PROVIDER_SETTINGS_PATH: path.join(work, "cline_providers.json"),
    },
  });
} catch (err) {
  failed = true;
  console.error(err instanceof Error ? err.message : err);
} finally {
  await fake.close();
  if (failed) console.error(`Left the scratch dir for inspection: ${work}`);
  else cleanup(work);
}
process.exit(failed ? 1 : 0);
