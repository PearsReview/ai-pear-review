import * as path from "node:path";
import * as vscode from "vscode";

import { AudioPlayer } from "../audio/player.ts";
import type { Backend } from "../backend/backend.ts";
import { repositoryRoots, reviewLocation } from "../git.ts";
import { showError } from "../log.ts";
import type { ReadingSpot } from "../review/markdownReading.ts";
import { blockAtFraction, type SpokenBlock } from "../review/reading.ts";
import { publish } from "../testProbe.ts";
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

interface Read extends ReadingState {
  // The file's text when the read began: the preview of this file is the one rendering it.
  text: string;
  clips: Map<number, { blocks: SpokenBlock[]; startLine: number; endLine: number }>;
  received: number;
  total: number | undefined;
  finished: number;
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
    if (!reveal) return;
    editor.revealRange(range, vscode.TextEditorRevealType.InCenterIfOutsideViewport);
    // A collapsed cursor at the passage: VS Code's markdown preview marks the cursor's
    // line and scrolls with the editor, so a preview open beside it follows the reading
    // (extensions can't highlight inside the preview itself). Collapsed, it never reads
    // as a selection for the chat's context.
    const start = range.start;
    if (!editor.selection.isEmpty || !editor.selection.active.isEqual(start)) {
      editor.selection = new vscode.Selection(start, start);
    }
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

  // A markdown preview of the file, open in a visible tab group. Its tab says
  // "Preview <file name>".
  const previewShowing = (filePath: string): boolean => {
    const name = path.basename(filePath);
    return vscode.window.tabGroups.all.some((group) => {
      const tab = group.activeTab;
      return (
        tab?.input instanceof vscode.TabInputWebview &&
        tab.input.viewType.includes("markdown.preview") &&
        tab.label.replace(/^\[?Preview\]? ?/, "").trim() === name
      );
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

  const stopReading = (): void => {
    if (!read) return;
    player.stop();
    // Still synthesising the rest of the file: stop that too.
    if (read.total === undefined || read.received < read.total) {
      try {
        backend.send("stop", {});
      } catch {
        // The backend is gone; nothing left to stop.
      }
    }
    finish();
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

  // The Explorer and editor menus pass the file's URI; a markdown preview passes nothing
  // and its tab is labelled "Preview <file name>", so the file is looked for in open
  // documents, then open tabs, then the workspace; otherwise the active editor's.
  const targetUri = async (arg: unknown): Promise<vscode.Uri | undefined> => {
    if (arg instanceof vscode.Uri) return arg;
    const tab = vscode.window.tabGroups.activeTabGroup.activeTab;
    if (tab?.input instanceof vscode.TabInputWebview && tab.input.viewType.includes("markdown.preview")) {
      const name = tab.label.replace(/^\[?Preview\]? ?/, "").trim();
      const named = (uri: vscode.Uri): boolean => path.basename(uri.fsPath) === name;
      const doc = vscode.workspace.textDocuments.find((d) => d.uri.scheme === "file" && named(d.uri));
      if (doc) return doc.uri;
      const tabUri = vscode.window.tabGroups.all
        .flatMap((g) => g.tabs)
        .map((t) => (t.input instanceof vscode.TabInputText ? t.input.uri : undefined))
        .find((u) => u && u.scheme === "file" && named(u));
      if (tabUri) return tabUri;
      const found = await vscode.workspace.findFiles(`**/${name}`, "**/node_modules/**", 2);
      if (found.length === 1) return found[0];
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
    const repos = await repositoryRoots();
    const root = repos.filter((r) => reviewLocation(uri, r)).sort((a, b) => b.length - a.length)[0];
    if (!root) {
      showError("That file isn't in a git repository VS Code has open.");
      return undefined;
    }
    if (backend.state === "ready" && backend.repoPath && path.relative(backend.repoPath, root) !== "") {
      showError(
        `Pear Review is running for ${path.basename(backend.repoPath)}. Stop it first to use another repository.`,
      );
      return undefined;
    }
    if (backend.state !== "ready") {
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
  const readFileAloud = async (arg: unknown): Promise<void> => {
    const uri = await targetUri(arg);
    if (!uri || uri.scheme !== "file" || !/\.(md|markdown)$/i.test(uri.fsPath)) {
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
    // A selection in that file reads just the blocks it touches.
    const editor = vscode.window.activeTextEditor;
    const selected =
      editor && editor.document.uri.toString() === uri.toString() && !editor.selection.isEmpty
        ? lineSpan(editor.selection)
        : undefined;
    const text = (await vscode.workspace.openTextDocument(uri)).getText();
    read = { filePath, uri, text, status: "loading", clips: new Map(), received: 0, total: undefined, finished: 0 };
    update();
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
      // Speech that never comes (the speech service is down, nothing readable): the
      // backend says so with an error, which ends a read still waiting for it.
      backend.on("error", ({ message }) => {
        if (read?.status === "loading" && read.received === 0 && /read|speech/i.test(message)) finish();
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
