import assert from "node:assert/strict";
import { test } from "node:test";

import MarkdownIt from "markdown-it";

import { readingPlugin, type ReadingSpot } from "../../src/review/markdownReading.ts";

const NOTES =
  "# Demo notes\n\nThis repo holds a tiny calculator.\n\n## Usage\n\n- Call add with two numbers.\n- Read the result.\n";

// `file` is what VS Code would name as env.currentDocument; undefined is what 1.140 gives.
function render(spot: Omit<ReadingSpot, "text"> | undefined, file?: string, source = NOTES): string {
  const md = new MarkdownIt();
  readingPlugin(
    md,
    () => spot && { ...spot, text: NOTES },
    (a, b) => a === b,
  );
  return md.render(source, { currentDocument: file ? { fsPath: file } : undefined });
}

void test("the block being read is marked in the preview", () => {
  const html = render({ fsPath: "/repo/NOTES.md", startLine: 3, endLine: 3 }, "/repo/NOTES.md");
  assert.match(html, /<p class="pear-reading">This repo holds a tiny calculator\.<\/p>/);
  assert.equal(html.match(/pear-reading/g)?.length, 1);
});

void test("a list item's paragraph is marked, not the whole list", () => {
  const html = render({ fsPath: "/repo/NOTES.md", startLine: 8, endLine: 8 }, "/repo/NOTES.md");
  assert.match(html, /<li class="pear-reading">Read the result\.<\/li>/);
  assert.doesNotMatch(html, /<ul class="pear-reading">/);
});

void test("another file's preview, or no read, is left alone", () => {
  const spot = { fsPath: "/repo/NOTES.md", startLine: 3, endLine: 3 };
  assert.doesNotMatch(render(spot, "/repo/OTHER.md"), /pear-reading/, "named as another file");
  assert.doesNotMatch(render(spot, undefined, "# Other\n\nDifferent text.\n"), /pear-reading/, "unnamed, other text");
  assert.doesNotMatch(render(undefined), /pear-reading/);
});

void test("with the document unnamed, the file being read is known by its text", () => {
  assert.match(
    render({ fsPath: "/repo/NOTES.md", startLine: 3, endLine: 3 }, undefined, NOTES.replace(/\n/g, "\r\n")),
    /pear-reading/,
  );
});
