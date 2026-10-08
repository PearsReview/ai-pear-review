// The GitHub Pull Requests extension's diff documents for a checked-out PR: one side is
// the checkout (file://), the other a "review:" document whose query is JSON naming the
// file and commit ({path, commit, base, rootPath, ...}); base marks the PR's base side.
// Pure, so it's unit-tested (test/unit/reviewUri.test.ts).
export const REVIEW_SCHEME = "review";

export interface ReviewQuery {
  // As the extension wrote it: an absolute path in the checkout, or one relative to it.
  path: string;
  side: "new" | "old";
}

export function parseReviewQuery(query: string, fallbackPath: string): ReviewQuery | undefined {
  let parsed: { path?: unknown; base?: unknown };
  try {
    parsed = JSON.parse(query) as typeof parsed;
  } catch {
    return undefined;
  }
  if (typeof parsed !== "object" || parsed === null) return undefined;
  const path = typeof parsed.path === "string" && parsed.path ? parsed.path : fallbackPath;
  return path ? { path, side: parsed.base === true ? "old" : "new" } : undefined;
}
