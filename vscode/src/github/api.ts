// The slice of GitHub's REST API a pull request review needs, over plain fetch (no
// dependency). PEAR_REVIEW_GITHUB_API points it at the integration tests' fake.
import type { GithubReviewComment, ReviewEvent } from "./reviewComments.ts";

const API = process.env.PEAR_REVIEW_GITHUB_API || "https://api.github.com";

export interface PullRequest {
  number: number;
  title: string;
  html_url: string;
  user: { login: string } | null;
  draft?: boolean;
  head: { sha: string; ref: string; label: string };
  base: { sha: string; ref: string };
}

export interface PullFile {
  filename: string;
  // Absent for a binary file, or one too large for GitHub to show.
  patch?: string;
}

export class GithubError extends Error {}

export class GithubClient {
  constructor(private readonly token: string) {}

  listOpenPulls(owner: string, repo: string): Promise<PullRequest[]> {
    return this.request(`/repos/${owner}/${repo}/pulls?state=open&per_page=100`);
  }

  getPull(owner: string, repo: string, number: number): Promise<PullRequest> {
    return this.request(`/repos/${owner}/${repo}/pulls/${number}`);
  }

  // Every changed file, across pages (GitHub sends at most 100 per page, 3000 in all).
  async listFiles(owner: string, repo: string, number: number): Promise<PullFile[]> {
    const files: PullFile[] = [];
    for (let page = 1; ; page++) {
      const batch = await this.request<PullFile[]>(
        `/repos/${owner}/${repo}/pulls/${number}/files?per_page=100&page=${page}`,
      );
      files.push(...batch);
      if (batch.length < 100) return files;
    }
  }

  // Submitted at once, comments and verdict together: nothing is left pending on the PR.
  createReview(
    owner: string,
    repo: string,
    number: number,
    review: { commit_id: string; event: ReviewEvent; body: string; comments: GithubReviewComment[] },
  ): Promise<{ html_url: string }> {
    return this.request(`/repos/${owner}/${repo}/pulls/${number}/reviews`, { method: "POST", body: review });
  }

  private async request<T>(path: string, options: { method?: string; body?: unknown } = {}): Promise<T> {
    const response = await fetch(`${API}${path}`, {
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
