// A git remote's URL → the GitHub repository it points at, on github.com or a GitHub
// Enterprise host. Pure, so it's unit-tested (test/unit/githubRemote.test.ts).

// Where a repository's pull requests live: the host its remotes name, the REST API
// that serves it, and the VS Code sign-in that gets a token for it.
export interface GithubHost {
  name: string;
  api: string;
  authProvider: "github" | "github-enterprise";
}

export const GITHUB_COM: GithubHost = { name: "github.com", api: "https://api.github.com", authProvider: "github" };

// The host VS Code's `github-enterprise.uri` setting names, or undefined when it's
// unset or not a URL. GitHub Enterprise Server serves its API under /api/v3; GitHub
// Enterprise Cloud with data residency (*.ghe.com) on an api. subdomain.
export function enterpriseHost(uri: string | undefined): GithubHost | undefined {
  let url: URL;
  try {
    url = new URL((uri ?? "").trim());
  } catch {
    return undefined;
  }
  const name = url.hostname.toLowerCase();
  if (!/^https?:$/.test(url.protocol) || !name || name === GITHUB_COM.name) return undefined;
  const api = name.endsWith(".ghe.com") ? `https://api.${name}` : `${url.origin}/api/v3`;
  return { name, api, authProvider: "github-enterprise" };
}

export interface GithubRepo {
  host: GithubHost;
  owner: string;
  repo: string;
}

// https://host/o/r(.git), git@host:o/r(.git), ssh://git@host(:port)/o/r(.git).
// Anything else (a host not in `hosts`, a local path) is not a GitHub remote.
const PATTERNS = [
  /^https?:\/\/(?:[^@/]+@)?([^/:@]+)(?::\d+)?\/([^/]+)\/([^/]+?)(?:\.git)?\/?$/i,
  /^ssh:\/\/git@([^/:@]+)(?::\d+)?\/([^/]+)\/([^/]+?)(?:\.git)?\/?$/i,
  /^git@([^/:@]+):([^/]+)\/([^/]+?)(?:\.git)?\/?$/i,
];

export function parseGithubRemote(url: string, hosts: GithubHost[] = [GITHUB_COM]): GithubRepo | undefined {
  for (const pattern of PATTERNS) {
    const [, name, owner, repo] = pattern.exec(url.trim()) ?? [];
    const host = hosts.find((h) => h.name === name?.toLowerCase());
    if (host && owner && repo) return { host, owner, repo };
  }
  return undefined;
}
