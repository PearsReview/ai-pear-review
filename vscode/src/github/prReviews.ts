// The pull requests under review, by the worktree each is checked out in. A worktree is
// a repository like any other to the rest of the extension (BackendManager, the
// Changes tree); these few places ask here whether it's a PR review:
// - the backend's start, for the base commit and read-only flag (backend.ts),
// - the old side of the diff, the merge-base rather than HEAD (git.ts),
// - comments, which go to GitHub instead of a plan (ui/pullRequests.ts, ui/comments.ts).
import * as path from "node:path";

import type { GithubHost } from "./remote.ts";

export interface PrReview {
  host: GithubHost;
  owner: string;
  repo: string;
  number: number;
  title: string;
  url: string;
  // The PR's head commit, checked out in the worktree, and the merge-base it's diffed
  // against.
  headSha: string;
  baseSha: string;
  worktree: string;
  // The reviewer's repository the worktree was made from.
  sourceRepo: string;
}

const reviews = new Map<string, PrReview>();

function keyOf(dir: string): string {
  const normal = path.resolve(dir);
  return process.platform === "win32" ? normal.toLowerCase() : normal;
}

export function addPrReview(review: PrReview): void {
  reviews.set(keyOf(review.worktree), review);
}

export function removePrReview(worktree: string): void {
  reviews.delete(keyOf(worktree));
}

export function prReviewFor(repoPath: string | undefined): PrReview | undefined {
  return repoPath ? reviews.get(keyOf(repoPath)) : undefined;
}

export function allPrReviews(): PrReview[] {
  return [...reviews.values()];
}

// What the backend process is started with for a PR review (app/web/config.py).
export function backendEnv(repoPath: string): Record<string, string> {
  const review = prReviewFor(repoPath);
  return review ? { REVIEW_BASE_SHA: review.baseSha, REVIEW_READ_ONLY: "1" } : {};
}
