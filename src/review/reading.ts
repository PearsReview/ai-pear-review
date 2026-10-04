// Which block of a file the voice is on. The speech service returns one clip per chunk
// with no word timings, so the position inside a clip is estimated from how far through
// it the player is, spread across its blocks by their spoken length (weights measured
// server-side on the text actually spoken). Exact again at every clip boundary. The same
// arithmetic as the browser's blockAtFraction (backend/static/js/audio.js). Pure, so it's
// unit-tested (test/unit/reading.test.ts).

export interface SpokenBlock {
  block_index: number;
  start_line: number;
  end_line: number;
  weight: number;
}

export function blockAtFraction<T extends { weight: number }>(blocks: T[], fraction: number): T | undefined {
  if (!blocks.length) return undefined;
  const total = blocks.reduce((sum, b) => sum + (b.weight || 0), 0);
  if (total <= 0) return blocks[0];
  const target = Math.min(1, Math.max(0, fraction)) * total;
  let acc = 0;
  for (const b of blocks) {
    acc += b.weight || 0;
    if (target <= acc) return b;
  }
  return blocks[blocks.length - 1];
}
