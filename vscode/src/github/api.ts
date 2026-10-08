// The slice of GitHub's REST API a pull request review needs (the PR's base, for its
// merge-base), over plain fetch (no
// dependency), on github.com or a GitHub Enterprise host. PEAR_REVIEW_GITHUB_API points
// it at the integration tests' fake.
import type { GithubHost } from "./remote.ts";

export interface PullRequest {
  number: number;
  title: string;
  html_url: string;
  user: { login: string } | null;
  draft?: boolean;
  head: { sha: string; ref: string; label: string };
  base: { sha: string; ref: string };
}

export class GithubError extends Error {}

export class GithubClient {
  private readonly api: string;

  constructor(
    host: GithubHost,
    private readonly token: string,
  ) {
    this.api = process.env.PEAR_REVIEW_GITHUB_API || host.api;
  }

  getPull(owner: string, repo: string, number: number): Promise<PullRequest> {
    return this.request(`/repos/${owner}/${repo}/pulls/${number}`);
  }

  private async request<T>(path: string, options: { method?: string; body?: unknown } = {}): Promise<T> {
    const response = await fetch(`${this.api}${path}`, {
      method: options.method ?? "GET",
      headers: {
        Accept: "application/vnd.github+json",
        Authorization: `Bearer ${this.token}`,
        "X-GitHub-Api-Version": "2022-11-28",
        ...(options.body ? { "Content-Type": "application/json" } : {}),
      },
      body: options.body ? JSON.stringify(options.body) : undefined,
    });
    if (!response.ok) {
      // GitHub's error body names what it rejected ("Unprocessable Entity" alone doesn't).
      const detail = await response.text().catch(() => "");
      throw new GithubError(
        `GitHub ${options.method ?? "GET"} ${path.split("?")[0]} failed (${response.status}): ${detail}`,
      );
    }
    return (await response.json()) as T;
  }
}
