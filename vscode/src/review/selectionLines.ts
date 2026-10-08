// Text selected in VS Code's markdown preview → the source lines it came from, so Read
// Aloud can read just that part. The rendered text has lost the markdown syntax (#, -,
// **, link targets), so only letters and digits take part in the match, as the
// read-along does (media/chat/readalong.js). When the whole selection doesn't match in
// one run (a link's address sits between its words in the source), its start and its
// end are found separately. Pure, so it's unit-tested (test/unit/selectionLines.test.ts).

const MATCHABLE = /[\p{L}\p{N}]/u;
// How much of each end is matched when the whole selection isn't one run.
const END_LENGTH = 24;

export interface LineRange {
  // 1-based, inclusive.
  startLine: number;
  endLine: number;
}

function letters(text: string): string {
  return Array.from(text)
    .filter((c) => MATCHABLE.test(c))
    .join("");
}

export function sourceLinesOf(source: string, selected: string): LineRange | undefined {
  const needle = letters(selected);
  if (!needle) return undefined;
  // Every matchable character of the source, with the line it's on.
  let text = "";
  const lineOf: number[] = [];
  source.split(/\r?\n/).forEach((line, i) => {
    for (const c of line) {
      if (!MATCHABLE.test(c)) continue;
      text += c;
      lineOf.push(i + 1);
    }
  });
  const whole = text.indexOf(needle);
  if (whole >= 0) return { startLine: lineOf[whole] ?? 1, endLine: lineOf[whole + needle.length - 1] ?? 1 };
  const head = needle.slice(0, END_LENGTH);
  const tail = needle.slice(-END_LENGTH);
  const from = text.indexOf(head);
  if (from < 0) return undefined;
  const to = text.indexOf(tail, from + head.length);
  return to < 0 ? undefined : { startLine: lineOf[from] ?? 1, endLine: lineOf[to + tail.length - 1] ?? 1 };
}
