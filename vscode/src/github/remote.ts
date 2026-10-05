// A git remote's URL → the GitHub repository it points at. Pure, so it's unit-tested
// (test/unit/githubRemote.test.ts).

export interface GithubRepo {
  owner: string;
  repo: string;
}

// https://github.com/o/r(.git), git@github.com:o/r(.git), ssh://git@github.com/o/r(.git).
// Anything else (GitLab, a GitHub Enterprise host, a local path) is not a GitHub remote.
const PATTERNS = [
  /^https?:\/\/(?:[^@/]+@)?github\.com\/([^/]+)\/([^/]+?)(?:\.git)?\/?$/i,
  /^(?:ssh:\/\/)?git@github\.com[:/]([^/]+)\/([^/]+?)(?:\.git)?\/?$/i,
];

export function parseGithubRemote(url: string): GithubRepo | undefined {
  for (const pattern of PATTERNS) {
    const [, owner, repo] = pattern.exec(url.trim()) ?? [];
    if (owner && repo) return { owner, repo };
  }
  return undefined;
}
