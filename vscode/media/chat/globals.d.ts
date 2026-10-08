// The chat webview's globals, for type-checking its scripts (tsconfig.webview.json):
// VS Code's webview API, and what each script exports for the next. They load in order:
// blocks.js, readalong.js, audio.js, chat.js (src/ui/chatHtml.ts).

interface VsCodeApi {
  postMessage(message: unknown): void;
  getState(): unknown;
  setState(state: unknown): void;
}

declare function acquireVsCodeApi(): VsCodeApi;

// A speaker button's look: see SPEAK_LOOK in chat.js.
type SpeakMode = "idle" | "loading" | "playing" | "paused";

// A clip's sentences and their spoken weight (speech_text.sentence_segments).
interface SentenceWeight {
  text: string;
  weight: number;
}

interface ResolvedSentence {
  block_index: number;
  weight: number;
  range: Range | null;
}

interface PearReadAlong {
  resolveSentences(root: Element, sentences: SentenceWeight[]): ResolvedSentence[];
  blockAtFraction(blocks: { block_index: number; weight: number }[], fraction: number): number | null;
  highlight(range: Range | null): void;
}

interface ChatAudio {
  readonly owner: HTMLElement | null;
  enqueue(
    payload: { chunk_index: number; audio_base64: string; mime_type: string; sentences?: unknown },
    button: HTMLElement | null,
  ): void;
  pause(): void;
  resume(): void;
  stop(): void;
  claim(button: HTMLElement): void;
}

interface Window {
  PearBlocks: { renderBlocks(el: HTMLElement, blocks: unknown[]): void };
  PearReadAlong: PearReadAlong;
  PearAudio: {
    createAudio(deps: {
      post: (message: Record<string, unknown>) => void;
      readAlong: PearReadAlong;
      setSpeakState: (button: HTMLElement, mode: SpeakMode) => void;
    }): ChatAudio;
  };
}
