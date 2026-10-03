// The read-along: which sentence of a reply (or which block of a file) the voice is on.
// Ported from backend/static/js/audio.js (buildTextIndex, findSentence,
// extendOverClosing, blockAtFraction), which can't be loaded here: that module imports
// the browser app's state. Keep the two in step.
//
// The speech service returns one audio clip per chunk with no word timings, so the
// position is estimated: how far through the clip we are, spread across its sentences
// by spoken length (weights measured server-side on the text actually spoken). It
// drifts a little mid-clip and is exact again at every clip boundary.
// @ts-check
(function () {
  // Drawn with the CSS Custom Highlight API, not by wrapping sentences in elements: a
  // sentence can start in plain text and end inside `code` or **bold**, which no single
  // element can cover. Without the API there is simply no highlight.
  const CAN_HIGHLIGHT = typeof CSS !== "undefined" && !!CSS.highlights && typeof Highlight === "function";
  const NAME = "pear-reading";
  let current = /** @type {Range | null} */ (null);

  function highlight(range) {
    if (range === current) return;
    current = range;
    if (!CAN_HIGHLIGHT) return;
    if (range) CSS.highlights.set(NAME, new Highlight(range));
    else CSS.highlights.delete(NAME);
  }

  // Only letters and digits take part in the match: the spoken text differs from the
  // rendered one only in whitespace, punctuation and symbols (see audio.js).
  const MATCHABLE = /[\p{L}\p{N}]/u;
  const CLOSING = /[.,;:!?()[\]{}"'’”]/;

  function buildTextIndex(root) {
    let text = "";
    const where = [];
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      const data = /** @type {Text} */ (node).data;
      for (let i = 0; i < data.length; i++) {
        if (!MATCHABLE.test(data[i])) continue;
        text += data[i];
        where.push([node, i]);
      }
    }
    return { root, text, where, cursor: 0 };
  }

  function extendOverClosing(root, node, offset) {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    walker.currentNode = node;
    for (;;) {
      while (offset < node.data.length && CLOSING.test(node.data[offset])) offset++;
      if (offset < node.data.length) return [node, offset];
      const next = /** @type {Text | null} */ (walker.nextNode());
      if (!next || !next.data.length || !CLOSING.test(next.data[0])) return [node, offset];
      node = next;
      offset = 0;
    }
  }

  function findSentence(index, sentence) {
    const needle = Array.from(String(sentence))
      .filter((c) => MATCHABLE.test(c))
      .join("");
    const at = needle ? index.text.indexOf(needle, index.cursor) : -1;
    if (at < 0) return null;
    index.cursor = at + needle.length;
    const [startNode, startOffset] = index.where[at];
    const [endNode, endOffset] = index.where[at + needle.length - 1];
    const range = document.createRange();
    range.setStart(startNode, startOffset);
    const [node, offset] = extendOverClosing(index.root, endNode, endOffset + 1);
    range.setEnd(node, offset);
    return range;
  }

  // Each sentence of a clip as {block_index, weight, range}; a sentence not found in the
  // rendered reply (a code block is spoken as "code block omitted") keeps its weight, so
  // the ones either side still line up with the audio.
  function resolveSentences(root, sentences) {
    if (!Array.isArray(sentences) || !root) return [];
    const index = buildTextIndex(root);
    return sentences.map((s, i) => ({ block_index: i, weight: s.weight, range: findSentence(index, s.text) }));
  }

  function blockAtFraction(blocks, fraction) {
    if (!blocks || !blocks.length) return null;
    const total = blocks.reduce((sum, b) => sum + (b.weight || 0), 0);
    if (total <= 0) return blocks[0].block_index;
    const target = fraction * total;
    let acc = 0;
    for (const b of blocks) {
      acc += b.weight || 0;
      if (target <= acc) return b.block_index;
    }
    return blocks[blocks.length - 1].block_index;
  }

  // @ts-ignore — the one global this file exports, read by chat.js.
  window.PearReadAlong = { resolveSentences, blockAtFraction, highlight };
})();
