// What the integration tests run against: a throwaway git repo seeded with every kind
// of change the review handles, and a copy of the backend whose config points at the
// fakes. Both live under a fresh temp directory per run, like qa_agent's scratch_repo
// and app_copy_dir; the repo's backend and its config are never written to.
import { execFileSync } from "node:child_process";
import { cpSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import * as path from "node:path";

import { FAKE_MODEL } from "./fakeServices.ts";

const ROOT = path.resolve(import.meta.dirname, "..", "..");
// The backend is the repo root this extension lives in (vscode/ is one level down).
export const BACKEND = path.resolve(ROOT, "..");

function git(cwd: string, ...args: string[]): void {
  execFileSync("git", args, { cwd, stdio: "pipe" });
}

function write(repo: string, file: string, content: string): void {
  const full = path.join(repo, file);
  mkdirSync(path.dirname(full), { recursive: true });
  // Bytes with \n, so Windows checkouts don't turn line numbers into a moving target.
  writeFileSync(full, Buffer.from(content, "utf8"));
}

const funcs = (skip: number[] = []): string =>
  Array.from({ length: 30 }, (_, i) => i)
    .filter((i) => !skip.includes(i))
    .map((i) => `def f${i}():\n    return ${i}\n\n`)
    .join("");

// The changes, and the hunk order the backend gives them (tracked files by path, then
// untracked): calc.py (added lines), old.py (deleted file), sample.py (the fake agent's
// file), src/funcs.py (a removal-only hunk), new_module.py (untracked, all added).
export function seedRepo(repo: string): void {
  mkdirSync(repo, { recursive: true });
  git(repo, "init", "-q", "-b", "main");
  git(repo, "config", "user.email", "tests@example.com");
  git(repo, "config", "user.name", "Pear Tests");
  git(repo, "config", "core.autocrlf", "false");
  // The backend adds its own folders to .git/info/exclude on start; doing it up front
  // keeps the settings file below from ever showing up as a change to review.
  write(repo, path.join(".git", "info", "exclude"), ".review/\n.briefing/\n.context/\n");
  write(repo, "calc.py", "def add(a, b):\n    return a + b\n");
  write(repo, "old.py", "print('remove me')\n");
  write(repo, "sample.py", 'def greet(name):\n    return f"Hello, {name}!"\n');
  write(repo, "src/funcs.py", funcs());
  write(
    repo,
    "NOTES.md",
    "# Demo notes\n\nThis repo holds a tiny calculator.\n\n## Usage\n\n- Call add with two numbers.\n- Read the result.\n",
  );
  git(repo, "add", ".");
  git(repo, "commit", "-qm", "initial");

  write(repo, "calc.py", 'def add(a, b):\n    """Add two numbers."""\n    return a + b\n');
  git(repo, "rm", "-q", "old.py");
  write(
    repo,
    "sample.py",
    'def greet(name):\n    return f"Hello, {name}!"\n\n\ndef farewell(name):\n    return f"Goodbye, {name}."\n',
  );
  write(repo, "src/funcs.py", funcs([20]));
  write(repo, "new_module.py", "VALUE = 42\n");
}

export interface BackendOptions {
  fakeUrl: string;
  // true: the reviewer model is the developer's real Ollama (PEAR_TEST_MODEL=ollama).
  liveModel: boolean;
  python: string;
}

// A copy of the backend with the coding agent swapped for tests/fake_acp_agent.py, as
// qa_agent's app_server does, plus the repo-level settings that point the model and the
// speech service at the fakes (the same keys the settings panel saves).
export function prepareBackend(work: string, repo: string, options: BackendOptions): string {
  const copy = path.join(work, "backend");
  for (const part of ["app", "static", "run.py", path.join("tests", "fake_acp_agent.py")]) {
    cpSync(path.join(BACKEND, part), path.join(copy, part), {
      recursive: true,
      filter: (src) => !src.includes("__pycache__"),
    });
  }

  const configPath = path.join(copy, "app", "config.yaml");
  const config = readFileSync(configPath, "utf8");
  const agent = path.join(copy, "tests", "fake_acp_agent.py").replaceAll("\\", "/");
  const python = options.python.replaceAll("\\", "/");
  const patched = config
    .replace("  agent: none  # none | cline", "  agent: cline")
    .replace('command: ["cline", "--acp"]', `command: ['${python}', '${agent}', 'comment']`);
  if (!patched.includes("agent: cline") || !patched.includes("fake_acp_agent.py")) {
    throw new Error("backend/app/config.yaml's harness block has changed shape; update test/integration/fixtures.ts.");
  }
  writeFileSync(configPath, patched);

  writeRepoSettings(repo, options);

  // The backend reads the agent's model from Cline's settings file; point it at a
  // fixture, never the developer's real one, which holds real keys.
  write(
    work,
    "cline_providers.json",
    JSON.stringify({
      lastUsedProvider: "anthropic",
      providers: { anthropic: { settings: { model: "claude-test", apiKey: "test-key-not-real" } } },
    }),
  );
  return copy;
}

// The repo-level settings that point the model and the speech service at the fakes
// (the same keys the settings panel saves). Every repository a run reviews needs them.
export function writeRepoSettings(repo: string, options: BackendOptions): void {
  const settings: Record<string, unknown> = {
    tts: { endpoint: `${options.fakeUrl}/speech` },
    stt: { endpoint: `${options.fakeUrl}/transcribe` },
  };
  if (!options.liveModel) {
    settings.provider = "ollama";
    settings.ollama = { base_url: options.fakeUrl, model: FAKE_MODEL };
  }
  write(repo, path.join(".review", "ui_settings.json"), JSON.stringify(settings, null, 2));
}

// A second, smaller repository for the multi-root run: one changed file.
export function seedSmallRepo(repo: string): void {
  mkdirSync(repo, { recursive: true });
  git(repo, "init", "-q", "-b", "main");
  git(repo, "config", "user.email", "tests@example.com");
  git(repo, "config", "user.name", "Pear Tests");
  git(repo, "config", "core.autocrlf", "false");
  write(repo, path.join(".git", "info", "exclude"), ".review/\n.briefing/\n.context/\n");
  write(repo, "tools.py", "def double(x):\n    return x * 2\n");
  git(repo, "add", ".");
  git(repo, "commit", "-qm", "initial");
  write(repo, "tools.py", "def double(x):\n    return x * 2\n\n\ndef triple(x):\n    return x * 3\n");
}

export function cleanup(work: string): void {
  rmSync(work, { recursive: true, force: true });
}
