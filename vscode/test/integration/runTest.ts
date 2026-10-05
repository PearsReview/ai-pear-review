// Runs the integration suite: a real VS Code with this extension, opened on a scratch
// repo, with a real backend behind it and fakes for everything that backend calls out to.
//
//   npm run test:integration          fake model (default)
//   npm run test:integration:ollama   your real local Ollama (or PEAR_TEST_MODEL=ollama)
//
// The coding agent and the speech service are always fakes; the microphone is a fake
// sounddevice that records a tone.
import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import * as path from "node:path";

import { runTests } from "@vscode/test-electron";

import { startFakeServices } from "./fakeServices.ts";
import { cleanup, prepareBackend, seedPullRequest, seedRepo, seedSmallRepo, writeRepoSettings } from "./fixtures.ts";

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

// A cold local model can take longer to load than the backend's 60 s call budget, and
// the first narration would fail on that alone. One tiny request loads it first.
async function warmUpOllama(backend: string, py: string): Promise<void> {
  const read =
    "import sys, yaml; c = yaml.safe_load(open(sys.argv[1], encoding='utf-8'))['conversation']['ollama']; print(c.get('base_url') or 'http://127.0.0.1:11434'); print(c['model'])";
  const [baseUrl, model] = execFileSync(py, ["-c", read, path.join(backend, "app", "config.yaml")], {
    encoding: "utf8",
  })
    .trim()
    .split(/\r?\n/);
  console.log(`Loading ${model} in Ollama before the run…`);
  const started = Date.now();
  await fetch(`${baseUrl}/api/generate`, {
    method: "POST",
    body: JSON.stringify({ model, prompt: "hi", stream: false, options: { num_predict: 1 } }),
  });
  console.log(`Model ready after ${Math.round((Date.now() - started) / 1000)}s.`);
}

const work = mkdtempSync(path.join(tmpdir(), "pear-it-"));
const repo = path.join(work, "repo");
const fake = await startFakeServices();
let failed = false;

// One VS Code instance per workspace, each with its own user data and test files
// (suite/index.ts picks them by PEAR_TEST_SUITE).
async function run(suite: string, workspace: string, backend: string, env: Record<string, string> = {}): Promise<void> {
  console.log(`\n— ${suite} run: ${workspace}`);
  // The interpreter is a setting, as a user without "Set Up Python Environment" sets it.
  const userData = path.join(work, `user-data-${suite}`);
  mkdirSync(path.join(userData, "User"), { recursive: true });
  writeFileSync(path.join(userData, "User", "settings.json"), JSON.stringify({ "pearReview.pythonPath": python }));
  await runTests({
    extensionDevelopmentPath: ROOT,
    extensionTestsPath: path.join(ROOT, "dist-test", "suite", "index.js"),
    launchArgs: [
      workspace,
      "--disable-extensions",
      "--disable-workspace-trust",
      "--skip-welcome",
      "--skip-release-notes",
      "--user-data-dir",
      userData,
    ],
    extensionTestsEnv: {
      PEAR_REVIEW_TEST: "1",
      PEAR_TEST_SUITE: suite,
      PEAR_REVIEW_BACKEND_DIR: backend,
      PEAR_TEST_FAKE_URL: fake.url,
      PEAR_TEST_LIVE_MODEL: liveModel ? "1" : "",
      PEAR_TEST_REPO: repo,
      PYTHONPATH: path.join(ROOT, "test", "integration", "fake_modules"),
      CLINE_PROVIDER_SETTINGS_PATH: path.join(work, "cline_providers.json"),
      ...env,
    },
  });
}

try {
  seedRepo(repo);
  const options = { fakeUrl: fake.url, liveModel, python };
  const backend = prepareBackend(work, repo, options);
  console.log(`Integration run: ${liveModel ? "real Ollama" : "fake model"}; scratch dir ${work}`);
  if (liveModel) await warmUpOllama(backend, python);
  await run("main", repo, backend);

  if (!liveModel) {
    // Two repositories in one window: each has its own backend and its own .review/.
    const multi = path.join(work, "multi");
    const first = path.join(multi, "first");
    const second = path.join(multi, "second");
    seedRepo(first);
    seedSmallRepo(second);
    writeRepoSettings(first, options);
    writeRepoSettings(second, options);
    const workspaceFile = path.join(multi, "two.code-workspace");
    writeFileSync(workspaceFile, JSON.stringify({ folders: [{ path: "first" }, { path: "second" }] }));
    await run("multi", workspaceFile, backend, { PEAR_TEST_REPO: first, PEAR_TEST_REPO_B: second });

    // A folder that isn't a git repository: the welcome says so, and nothing starts.
    const plain = path.join(work, "plain");
    mkdirSync(plain, { recursive: true });
    writeFileSync(path.join(plain, "notes.md"), "# Not a repository\n");
    await run("nogit", plain, backend, { PEAR_TEST_REPO: plain });

    // A GitHub pull request, reviewed read-only in a worktree, its comments posted to a
    // fake GitHub API.
    const pr = seedPullRequest(path.join(work, "pr"));
    writeRepoSettings(pr.source, options);
    await fetch(`${fake.url}/__github`, { method: "POST", body: JSON.stringify({ pull: pr.pull, files: pr.files }) });
    await run("pr", pr.source, backend, {
      PEAR_TEST_REPO: pr.source,
      PEAR_TEST_PR_MERGE_BASE: pr.mergeBase,
      PEAR_REVIEW_GITHUB_API: fake.url,
      PEAR_REVIEW_GITHUB_TOKEN: "test-token",
    });
  }
} catch (err) {
  failed = true;
  console.error(err instanceof Error ? err.message : err);
} finally {
  await fake.close();
  if (failed) console.error(`Left the scratch dir for inspection: ${work}`);
  else cleanup(work);
}
process.exit(failed ? 1 : 0);
