// The git side of a pull request review: which remotes are GitHub repositories, fetching
// a PR, and the detached worktree it's reviewed in, so the reviewer's own checkout is
// never touched (no checkout, no stash, no branch switch).
import { execFile } from "node:child_process";
import * as fs from "node:fs";
import * as path from "node:path";

import { log } from "../log.ts";
import { parseGithubRemote, type GithubHost, type GithubRepo } from "./remote.ts";

// Fetching a large PR over a slow connection takes a while; anything else is instant.
const GIT_TIMEOUT_MS = 120_000;

export function git(cwd: string, args: string[]): Promise<string> {
  return new Promise((resolve, reject) => {
    execFile(
      "git",
      args,
      { cwd, timeout: GIT_TIMEOUT_MS, windowsHide: true, maxBuffer: 16 * 1024 * 1024 },
      (err, stdout, stderr) => {
        if (err) reject(new Error(`git ${args[0]} failed: ${stderr.trim() || err.message}`));
        else resolve(stdout.trim());
      },
    );
  });
}

export interface GithubRemote extends GithubRepo {
  name: string;
}

// Each remote that points at one of `hosts`. Read from the config rather than `git remote
// get-url`, which applies url.*.insteadOf: the configured URL names the repository.
export async function githubRemotes(repo: string, hosts: GithubHost[]): Promise<GithubRemote[]> {
  const lines = await git(repo, ["config", "--get-regexp", String.raw`^remote\..*\.url$`]).catch(() => "");
  return lines.split("\n").flatMap((line) => {
    const [, name, url] = /^remote\.(.+)\.url\s+(.+)$/.exec(line.trim()) ?? [];
    const parsed = url ? parseGithubRemote(url, hosts) : undefined;
    return name && parsed ? [{ name, ...parsed }] : [];
  });
}

// Fetches the PR's head and its base branch from `remote`, then returns the merge-base:
// the commit GitHub's "Files changed" diffs against.
export async function fetchPull(
  repo: string,
  remote: string,
  pull: { number: number; headSha: string; baseRef: string; baseSha: string },
): Promise<string> {
  await git(repo, ["fetch", "--no-tags", remote, `refs/pull/${pull.number}/head`, `refs/heads/${pull.baseRef}`]);
  return git(repo, ["merge-base", pull.baseSha, pull.headSha]);
}

// The reviewer's settings for a repository live in it (app/services/settings_store.py).
const SETTINGS_FILE = path.join(".review", "ui_settings.json");

// A fresh detached worktree of `sha` at `dir`, replacing one left there before. It
// takes the source repository's review settings (model, voices), so the PR is reviewed
// the way that repository's changes are.
export async function addWorktree(repo: string, dir: string, sha: string): Promise<void> {
  if (fs.existsSync(dir)) await removeWorktree(dir);
  fs.mkdirSync(path.dirname(dir), { recursive: true });
  await git(repo, ["worktree", "add", "--detach", dir, sha]);
  const settings = path.join(repo, SETTINGS_FILE);
  if (fs.existsSync(settings)) {
    fs.mkdirSync(path.join(dir, ".review"), { recursive: true });
    fs.copyFileSync(settings, path.join(dir, SETTINGS_FILE));
  }
}

// --force: the review leaves its own working files (.review/, .briefing/) in the worktree.
// On Windows a folder can't be deleted while a process has it as its working directory:
// the backend just stopped, or a git the git extension started there, can hold it for a
// moment. The delete retries for a few seconds; a folder still held is left for the
// next start's cleanup (removeStaleWorktrees) rather than failing the review's end.
export async function removeWorktree(dir: string): Promise<void> {
  let mainRepo: string | undefined;
  try {
    mainRepo = path.dirname(await git(dir, ["rev-parse", "--path-format=absolute", "--git-common-dir"]));
    await git(mainRepo, ["worktree", "remove", "--force", dir]);
  } catch (err) {
    log(`Couldn't remove the worktree at ${dir} through git (${String(err)}); deleting the folder.`);
  }
  try {
    fs.rmSync(dir, { recursive: true, force: true, maxRetries: 20, retryDelay: 250 });
  } catch (err) {
    log(`Couldn't delete ${dir} (${String(err)}); it will be removed the next time VS Code starts.`);
  }
  // Drops git's record of a worktree whose folder is gone (or went with the delete).
  if (mainRepo) await git(mainRepo, ["worktree", "prune"]).catch(() => undefined);
}

// Worktrees a previous session left (VS Code closed mid-review): removed on startup.
export async function removeStaleWorktrees(root: string, inUse: (dir: string) => boolean): Promise<void> {
  if (!fs.existsSync(root)) return;
  for (const name of fs.readdirSync(root)) {
    const dir = path.join(root, name);
    if (!inUse(dir)) await removeWorktree(dir);
  }
}
