// Marks the passage being read aloud in VS Code's markdown preview. The preview renders
// with markdown-it, which extensions can extend (contributes."markdown.markdownItPlugins"):
// each block token knows its source lines (token.map), so the blocks the voice is on get
// the class "pear-reading", styled by media/preview/reading.css and scrolled to by
// media/preview/reading.js. Pure (no vscode import), so it's unit-tested with markdown-it
// itself (test/unit/markdownReading.test.ts).
// The slice of markdown-it this uses, described here so the extension needs no
// markdown-it types at runtime (VS Code passes its own instance).
interface Token {
  type: string;
  map: [number, number] | null;
  hidden: boolean;
  attrJoin(name: string, value: string): void;
}

export interface MarkdownItLike {
  core: { ruler: { push(name: string, rule: (state: { tokens: Token[]; env: unknown; src: string }) => void): void } };
}

// The passage, in 1-based inclusive lines, and which file it's in. VS Code's render
// context names the document (env.currentDocument) but leaves it empty, measured on
// 1.140, so the preview of the file being read is the one whose source is that file's
// text; the path is checked too when VS Code does give it.
export interface ReadingSpot {
  fsPath: string;
  text: string;
  startLine: number;
  endLine: number;
}

const normalize = (text: string): string => text.replace(/\r\n/g, "\n").trimEnd();

// Blocks, not their containers: a list item is marked, not the whole list. List items
// are on the list because a tight list renders no <p> for its paragraphs (the
// paragraph tokens are hidden), so marking those alone would show nothing.
const BLOCKS = new Set([
  "paragraph_open",
  "heading_open",
  "list_item_open",
  "fence",
  "code_block",
  "table_open",
  "hr",
  "html_block",
]);

export function readingPlugin(
  md: MarkdownItLike,
  current: () => ReadingSpot | undefined,
  samePath: (a: string, b: string) => boolean,
): void {
  md.core.ruler.push("pear_reading", (state) => {
    const spot = current();
    if (!spot) return;
    const doc = (state.env as { currentDocument?: { fsPath?: string } } | undefined)?.currentDocument;
    const thisFile = doc?.fsPath ? samePath(doc.fsPath, spot.fsPath) : normalize(state.src) === normalize(spot.text);
    if (!thisFile) return;
    for (const token of state.tokens) {
      if (!BLOCKS.has(token.type) || !token.map || token.hidden) continue;
      // token.map is [first line, line after the last], 0-based.
      const [from, to] = token.map;
      if (from < spot.endLine && to > spot.startLine - 1) token.attrJoin("class", "pear-reading");
    }
  });
}
