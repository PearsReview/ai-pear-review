// Queued review comments → a GitHub pull request review's comments. Pure, so it's
// unit-tested (test/unit/reviewComments.test.ts).
import type { ReviewComment, Severity } from "../backend/protocol.ts";
import { anchorRange } from "../review/anchors.ts";
import { parseHeader } from "../review/hunks.ts";

export type ReviewEvent = "COMMENT" | "REQUEST_CHANGES" | "APPROVE";

// One entry of POST /repos/{owner}/{repo}/pulls/{n}/reviews "comments". An inline
// comment has line/side (and start_line/start_side for a range); a file-level one has
// subject_type "file" and no line at all.
export interface GithubReviewComment {
  path: string;
  body: string;
  line?: number;
  side?: "LEFT" | "RIGHT";
  start_line?: number;
  start_side?: "LEFT" | "RIGHT";
  subject_type?: "file";
}

const SEVERITY_PREFIX: Record<Severity, string> = { "must-fix": "Must fix", suggestion: "Suggestion", nit: "Nit" };

// Each hunk of a file's GitHub patch as the line ranges it covers on each side. GitHub
// only takes an inline comment on a line inside one of these, and a range comment must
// stay inside a single hunk.
export interface PatchHunk {
  left: [number, number];
  right: [number, number];
}

export function patchHunks(patch: string | undefined): PatchHunk[] {
  if (!patch) return [];
  return patch.split("\n").flatMap((line) => {
    const range = parseHeader(line);
    if (!range) return [];
    return [
      {
        left: [range.oldStart, range.oldStart + range.oldCount - 1],
        right: [range.newStart, range.newStart + range.newCount - 1],
      },
    ];
  });
}

const within = ([from, to]: [number, number], line: number): boolean => line >= from && line <= to;

// `patches` maps a file path to its patch from GET .../pulls/{n}/files. A file with no
// patch there (too large, or binary) or lines outside its hunks gets a file-level
// comment that names the lines in its body, so where it pointed isn't lost.
export function toGithubComment(
  comment: ReviewComment,
  patches: ReadonlyMap<string, string | undefined>,
): GithubReviewComment {
  const { side, startLine, endLine } = anchorRange(comment.anchor);
  const githubSide = side === "new" ? "RIGHT" : "LEFT";
  const body = `**${SEVERITY_PREFIX[comment.severity]}:** ${comment.instruction}`;
  const hunk = patchHunks(patches.get(comment.file_path)).find((h) => {
    const range = side === "new" ? h.right : h.left;
    return within(range, startLine) && within(range, endLine);
  });
  if (comment.anchor && hunk) {
    return startLine === endLine
      ? { path: comment.file_path, body, line: endLine, side: githubSide }
      : {
          path: comment.file_path,
          body,
          line: endLine,
          side: githubSide,
          start_line: startLine,
          start_side: githubSide,
        };
  }
  const where = comment.anchor
    ? `${startLine === endLine ? `Line ${startLine}` : `Lines ${startLine}–${endLine}`}${side === "old" ? " (before)" : ""}: `
    : "";
  return { path: comment.file_path, body: `${where}${body}`, subject_type: "file" };
}
