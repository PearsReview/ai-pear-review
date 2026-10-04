// A queued comment's anchor → where its thread goes in the diff. Pure, so it's
// unit-tested (test/unit/anchors.test.ts).
import type { CommentAnchor } from "../backend/protocol.ts";

export interface AnchorRange {
  // "new" is the working file, "old" is HEAD.
  side: "new" | "old";
  // 1-based, inclusive.
  startLine: number;
  endLine: number;
}

// The side both ends of the anchor have line numbers on, preferring the working file.
// A HEAD-side selection that starts on unchanged context still lands on HEAD, because
// its last line (a removed one) has no working-file number. An anchor the browser made
// across both sides, or none at all (a whole-hunk comment), falls back to whatever
// working-file line exists, then to line 1.
export function anchorRange(anchor: CommentAnchor | null): AnchorRange {
  const a = anchor ?? { first_old_lineno: null, first_new_lineno: null, last_old_lineno: null, last_new_lineno: null };
  if (a.first_new_lineno !== null && a.last_new_lineno !== null) {
    return { side: "new", startLine: a.first_new_lineno, endLine: a.last_new_lineno };
  }
  if (a.first_old_lineno !== null && a.last_old_lineno !== null) {
    return { side: "old", startLine: a.first_old_lineno, endLine: a.last_old_lineno };
  }
  const line = a.first_new_lineno ?? a.last_new_lineno ?? 1;
  return { side: "new", startLine: line, endLine: line };
}
