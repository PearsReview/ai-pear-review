import * as path from "node:path";
import * as vscode from "vscode";

import { AudioPlayer } from "../audio/player.ts";
import type { Speaking } from "../audio/speaking.ts";
import type { Backend } from "../backend/backend.ts";
import { CANCELLING_MESSAGES } from "../backend/protocol.ts";
import { repoContaining, repositoryRoots, reviewLocation } from "../git.ts";
import { log, showError } from "../log.ts";
import type { ReadingSpot } from "../review/markdownReading.ts";
import { blockAtFraction, type SpokenBlock } from "../review/reading.ts";
import { sourceLinesOf, type LineRange } from "../review/selectionLines.ts";
import { publish } from "../testProbe.ts";
import { previewFile, previewShowing, previewTab } from "./previewTabs.ts";
import { lineSpan } from "./selection.ts";
import type { ChatTarget } from "./target.ts";

// Where a markdown file is being read aloud: the passage being read, for the highlight.
export interface ReadingPosition {
  filePath: string;
  // 1-based, inclusive.
  startLine: number;
  endLine: number;
}

// A read in progress: fetching speech, playing, or paused.
export interface ReadingState {
  filePath: string;
  uri: vscode.Uri;
  status: "loading" | "playing" | "paused";
}

export interface Reader {
  readonly state: ReadingState | undefined;
  readonly onDidChange: vscode.Event<ReadingState | undefined>;
  // The passage being read, for the markdown preview's plugin (markdownReading.ts).
  readonly spot: ReadingSpot | undefined;
}

// How long the clipboard gets to receive a copy from the preview's webview.
const COPY_SETTLE_MS = 150;

// The speech the player can play (python/player.py reads WAV).
const PLAYABLE = new Set(["audio/wav", "audio/x-wav", "audio/wave"]);

interface Read extends ReadingState {
  // The file's text when the read began: the preview of this file is the one rendering it.
  text: string;
  clips: Map<number, { blocks: SpokenBlock[]; startLine: number; endLine: number }>;
  received: number;
  // How many clips the read will have: unknown until the first arrives, and cut to what
  // has arrived when the backend stops making them (a cancel, or its finished notice).
  total: number | undefined;
  finished: number;
  // The player has nothing left to play right now.
  idle: boolean;
}

// Two ways into the repo beyond the changes under review: asking the reviewer about
// any file (explore_reply), and reading a markdown file aloud (speak_file).
//
// Read aloud is controlled where it's started: the speaker on the file's row (Changes
// view, Explorer) or its editor's title bar becomes pause, play and stop while it reads.
// The extension plays the audio itself (audio/player.ts) — a webview won't play sound
// before it has been clicked — and highlights the passage being read in the file's text.
export function register(
  context: vscode.ExtensionContext,
  backend: Backend,
  target: ChatTarget,
  speaking: Speaking,
): { reader: Reader; disposables: vscode.Disposable[] } {
  const player = new AudioPlayer(context.extensionPath);
  const changes = new vscode.EventEmitter<ReadingState | undefined>();
  const decoration = vscode.window.createTextEditorDecorationType({
    isWholeLine: true,
    backgroundColor: new vscode.ThemeColor("editor.findMatchBackground"),
    borderStyle: "solid",
    borderColor: new vscode.ThemeColor("editor.findMatchBorder"),
    borderWidth: "0 0 0 3px",
    overviewRulerColor: new vscode.ThemeColor("editor.findMatchBackground"),
    overviewRulerLane: vscode.OverviewRulerLane.Center,
  });
  let read: Read | undefined;
  let position: ReadingPosition | undefined;
  publish("files.reading", () => position);
  publish("files.read", () => read && { filePath: read.filePath, status: read.status });

  // --- the highlight ---------------------------------------------------------------------

  const decorate = (editor: vscode.TextEditor, reveal: boolean): void => {
    const root = backend.repoPath;
    const location = root ? reviewLocation(editor.document.uri, root) : undefined;
    if (!position || location?.side !== "new" || location.filePath !== position.filePath) {
      editor.setDecorations(decoration, []);
      return;
    }
    const range = new vscode.Range(position.startLine - 1, 0, position.endLine - 1, 0);
    editor.setDecorations(decoration, [range]);
    // Scrolls, but never moves the cursor or selection: the reviewer may be working in
    // the file while it's read. The preview follows on its own (markdownReading.ts).
    if (reveal) editor.revealRange(range, vscode.TextEditorRevealType.InCenterIfOutsideViewport);
  };

  // The highlight needs the file's text on screen. Read from a preview (or with the file
  // closed), the text opens beside it once per read, without taking focus.
  let textShownFor: string | undefined;
  const ensureTextVisible = async (filePath: string): Promise<void> => {
    const root = backend.repoPath;
    if (!root || textShownFor === filePath) return;
    textShownFor = filePath;
    const shown = vscode.window.visibleTextEditors.some(
      (e) => e.document.uri.scheme === "file" && reviewLocation(e.document.uri, root)?.filePath === filePath,
    );
    if (shown || previewShowing(filePath)) return;
    await vscode.window.showTextDocument(vscode.Uri.file(path.join(root, filePath)), {
      viewColumn: vscode.ViewColumn.Beside,
      preserveFocus: true,
      preview: true,
    });
  };

  // The preview marks the passage as it renders (markdownReading.ts), so it re-renders
  // as the reading moves.
  let refreshQueued = false;
  const refreshPreview = (filePath: string | undefined): void => {
    if (!filePath || !previewShowing(filePath) || refreshQueued) return;
    refreshQueued = true;
    setTimeout(() => {
      refreshQueued = false;
      void vscode.commands.executeCommand("markdown.preview.refresh");
    }, 50);
  };

  const highlight = (next: ReadingPosition | undefined): void => {
    const same =
      next &&
      position &&
      next.filePath === position.filePath &&
      next.startLine === position.startLine &&
      next.endLine === position.endLine;
    if (same) return;
    const was = position?.filePath;
    position = next;
    refreshPreview(next?.filePath ?? was);
    if (!next) {
      textShownFor = undefined;
      for (const editor of vscode.window.visibleTextEditors) decorate(editor, false);
      return;
    }
    void ensureTextVisible(next.filePath).then(() => {
      for (const editor of vscode.window.visibleTextEditors) decorate(editor, true);
    });
  };

  // --- the read's state, published as context keys for the menus' icons -----------------

  const update = (): void => {
    const state = read && { filePath: read.filePath, uri: read.uri, status: read.status };
    void vscode.commands.executeCommand("setContext", "pearReview.reading", !!read);
    void vscode.commands.executeCommand("setContext", "pearReview.readingStatus", read?.status ?? "");
    void vscode.commands.executeCommand("setContext", "pearReview.readingResources", read ? [read.uri.toString()] : []);
    changes.fire(state);
  };

  const finish = (): void => {
    read = undefined;
    highlight(undefined);
    update();
  };

  // The backend is still making speech for this read.
  const receiving = (r: Read): boolean => r.total === undefined || r.received < r.total;

  // No more clips are coming: the read ends once the player has played what it has.
  const noMoreClips = (): void => {
    if (!read) return;
    read.total = read.received;
    if (read.received === 0 || (read.idle && read.finished >= read.received)) finish();
  };

  const stopReading = (): void => {
    if (!read) return;
    const stillMaking = receiving(read);
    player.stop();
    finish();
    // Still synthesising the rest of the file (so speak_file is still the backend's
    // current task): stop that too. Sent only then, or it would cancel something else.
    if (stillMaking) {
      try {
        backend.send("stop", {});
      } catch {
        // The backend is gone; nothing left to stop.
      }
    }
  };

  const pauseReading = (): void => {
    if (read?.status !== "playing") return;
    player.pause();
    read.status = "paused";
    update();
  };

  const resumeReading = (): void => {
    if (read?.status !== "paused") return;
    player.resume();
    read.status = "playing";
    update();
  };

  // --- which file a command means ---------------------------------------------------------

  const activeTabIsPreview = (): boolean => previewTab() !== undefined;

  // The Explorer and editor menus pass the file's URI; a markdown preview passes nothing,
  // so its file is worked out from the tab (previewTabs.ts); otherwise the active editor's.
  const targetUri = async (arg: unknown): Promise<vscode.Uri | undefined> => {
    if (arg instanceof vscode.Uri && arg.scheme === "file") return arg;
    const tab = previewTab();
    if (tab) {
      const file = await previewFile(tab);
      if (file) return file;
      log(`Read Aloud: can't tell which file the preview "${tab.label}" shows.`);
      throw new Error(
        `Couldn't tell which file "${tab.label}" shows (more than one has that name). Open the file's text and use Read Aloud there.`,
      );
    }
    return vscode.window.activeTextEditor?.document.uri;
  };

  // Asking about a file works before a review starts, so this starts the backend for
  // the file's repository if it isn't running, without starting the review.
  const ensureBackendFor = async (uri: vscode.Uri): Promise<string | undefined> => {
    if (vscode.env.remoteName) {
      showError("Remote workspaces aren't supported. Open the repository locally.");
      return undefined;
    }
    const root = repoContaining(uri, await repositoryRoots());
    if (!root) {
      showError("That file isn't in a git repository VS Code has open.");
      return undefined;
    }
    // The file's repository becomes the one shown (each keeps its own backend).
    const shown = backend.repoPath && path.relative(backend.repoPath, root) === "";
    if (backend.state !== "ready" || !shown) {
      await vscode.window.withProgress(
        { location: vscode.ProgressLocation.Window, title: "Pear Review: starting backend" },
        () => backend.start(root),
      );
    }
    return reviewLocation(uri, root)?.filePath;
  };

  const askAboutFile = async (arg: unknown): Promise<void> => {
    const uri = await targetUri(arg);
    if (!uri || uri.scheme !== "file") {
      showError("Open or select a file in the repository first.");
      return;
    }
    const filePath = await ensureBackendFor(uri);
    if (!filePath) return;
    if (vscode.window.activeTextEditor?.document.uri.toString() !== uri.toString()) {
      await vscode.window.showTextDocument(uri, { preview: true });
    }
    target.setFile(filePath);
    await vscode.commands.executeCommand("pearReview.chat.focus");
  };

  // The speaker: reads the file, or, pressed on the file being read, pauses, resumes,
  // or (while speech is still being made) cancels.
  // The text selected in the markdown preview, as source lines. The preview can't tell
  // extensions about its selection, but VS Code's copy reaches a focused webview: copy
  // it, put the clipboard back, and find the copied text in the file. Undefined when
  // nothing is selected there (or copy didn't reach it), which reads the whole file.
  const previewSelection = async (source: string): Promise<LineRange | undefined> => {
    const before = await vscode.env.clipboard.readText();
    const marker = `pear-review-no-selection-${Date.now()}`;
    await vscode.env.clipboard.writeText(marker);
    try {
      await vscode.commands.executeCommand("editor.action.clipboardCopyAction");
      await new Promise((resolve) => setTimeout(resolve, COPY_SETTLE_MS));
      const copied = await vscode.env.clipboard.readText();
      if (!copied.trim() || copied === marker) {
        log("Read Aloud: nothing selected in the preview, so the whole file is read.");
        return undefined;
      }
      const range = sourceLinesOf(source, copied);
      log(
        range
          ? `Read Aloud: reading the preview's selection, lines ${range.startLine}-${range.endLine}.`
          : "Read Aloud: couldn't find the preview's selection in the file, so the whole file is read.",
      );
      return range;
    } finally {
      await vscode.env.clipboard.writeText(before);
    }
  };

  const readFileAloud = async (arg: unknown): Promise<void> => {
    const fromPreview = !(arg instanceof vscode.Uri && arg.scheme === "file") && activeTabIsPreview();
    const uri = await targetUri(arg);
    if (!uri || uri.scheme !== "file" || !/\.(md|markdown)$/i.test(uri.fsPath)) {
      // What the button passed, and what was found, for a report from the log.
      const passed = arg instanceof vscode.Uri ? arg.toString() : arg === undefined ? "nothing" : JSON.stringify(arg);
      const tabs = vscode.window.tabGroups.all.map((g) => g.activeTab?.label ?? "-").join(" | ");
      log(`Read Aloud: no markdown file found (passed ${passed}; found ${uri?.toString() ?? "none"}; tabs ${tabs}).`);
      showError("Read Aloud works on markdown (.md) files.");
      return;
    }
    if (read && read.uri.toString() === uri.toString()) {
      if (read.status === "playing") pauseReading();
      else if (read.status === "paused") resumeReading();
      else stopReading();
      return;
    }
    const filePath = await ensureBackendFor(uri);
    if (!filePath) return;
    stopReading();
    // A selection reads just the blocks it touches: in the file's text, or in its preview.
    const text = (await vscode.workspace.openTextDocument(uri)).getText();
    const editor = vscode.window.activeTextEditor;
    const selected =
      editor && editor.document.uri.toString() === uri.toString() && !editor.selection.isEmpty
        ? lineSpan(editor.selection)
        : fromPreview && vscode.workspace.getConfiguration("pearReview").get<boolean>("readPreviewSelection")
          ? await previewSelection(text)
          : undefined;
    read = {
      filePath,
      uri,
      text,
      status: "loading",
      clips: new Map(),
      received: 0,
      total: undefined,
      finished: 0,
      idle: true,
    };
    update();
    speaking.claim("file");
    backend.send(
      "speak_file",
      selected
        ? { file_path: filePath, start_line: selected.startLine, end_line: selected.endLine }
        : { file_path: filePath },
    );
  };

  const run = (fn: (arg: unknown) => Promise<void>) => (arg: unknown) =>
    fn(arg).catch((err: unknown) => showError(err instanceof Error ? err.message : String(err)));

  const reader: Reader = {
    get state() {
      return read && { filePath: read.filePath, uri: read.uri, status: read.status };
    },
    onDidChange: changes.event,
    get spot() {
      const root = backend.repoPath;
      return position && root && read
        ? {
            fsPath: path.join(root, position.filePath),
            text: read.text,
            startLine: position.startLine,
            endLine: position.endLine,
          }
        : undefined;
    },
  };

  return {
    reader,
    disposables: [
      player,
      changes,
      decoration,
      // Each clip of the file goes to the player as it arrives.
      backend.on("file_audio_chunk", (p) => {
        if (!read || p.file_path !== read.filePath) return;
        if (!PLAYABLE.has(p.mime_type.toLowerCase())) {
          showError(
            `Read Aloud plays WAV speech, and the text-to-speech service sends ${p.mime_type}. Choose a service that returns WAV in settings (⚙).`,
          );
          stopReading();
          return;
        }
        read.idle = false;
        const id = player.play(p.audio_base64);
        const blocks: SpokenBlock[] = p.blocks ?? [];
        read.clips.set(id, { blocks, startLine: p.start_line, endLine: p.end_line });
        read.received += 1;
        read.total = p.chunk_count;
      }),
      player.onEvent((e) => {
        if (!read) return;
        if (e.event === "error") {
          showError(e.message);
          finish();
          return;
        }
        if (e.event === "idle") {
          read.idle = true;
          if (read.total !== undefined && read.finished >= read.total) finish();
          return;
        }
        const clip = read.clips.get(e.id);
        if (!clip) return;
        if (e.event === "start") {
          if (read.status === "loading") {
            read.status = "playing";
            update();
          }
          const first = clip.blocks[0];
          highlight({
            filePath: read.filePath,
            startLine: first?.start_line ?? clip.startLine,
            endLine: first?.end_line ?? clip.endLine,
          });
        } else if (e.event === "progress") {
          const block = blockAtFraction(clip.blocks, e.fraction);
          if (block) highlight({ filePath: read.filePath, startLine: block.start_line, endLine: block.end_line });
        } else if (e.event === "done") {
          read.finished += 1;
        }
      }),
      // The read failed (nothing readable, speech off, the speech service down partway):
      // play what already came, then end.
      backend.on("error", ({ source }) => {
        if (source === "speak_file") noMoreClips();
      }),
      backend.on("notice", ({ event }) => {
        if (event === "reading_finished") noMoreClips();
      }),
      // Another long-running message cancels speak_file on the backend, silently: the read
      // ends with the clips it has.
      backend.onDidSend((type) => {
        if (read && receiving(read) && type !== "speak_file" && CANCELLING_MESSAGES.has(type)) noMoreClips();
      }),
      // The chat started speaking: one voice at a time.
      speaking.onDidClaim((who) => {
        if (who === "chat") stopReading();
      }),
      vscode.commands.registerCommand("pearReview.askAboutFile", run(askAboutFile)),
      vscode.commands.registerCommand("pearReview.readAloud", run(readFileAloud)),
      vscode.commands.registerCommand("pearReview.pauseReading", pauseReading),
      vscode.commands.registerCommand("pearReview.resumeReading", resumeReading),
      vscode.commands.registerCommand("pearReview.stopReading", stopReading),
      vscode.commands.registerCommand("pearReview.cancelReading", stopReading),
      vscode.commands.registerCommand("pearReview.openRepoFile", (filePath: unknown) => {
        if (typeof filePath === "string" && backend.repoPath) {
          void vscode.window.showTextDocument(vscode.Uri.file(path.join(backend.repoPath, filePath)));
        }
      }),
      // Decorations belong to an editor instance; reopening the file makes a new one.
      vscode.window.onDidChangeVisibleTextEditors((editors) => editors.forEach((e) => decorate(e, false))),
      backend.onStateChange((state) => {
        if (state !== "ready") stopReading();
      }),
    ],
  };
}
