import * as path from "node:path";
import * as vscode from "vscode";

import type { Backend } from "../backend/backend.ts";
import { repositoryRoots, reviewLocation } from "../git.ts";
import { showError } from "../log.ts";
import { publish } from "../testProbe.ts";
import { lineSpan } from "./selection.ts";
import type { ChatTarget } from "./target.ts";

// Where a markdown file is being read aloud: the clip playing now, for the highlight.
export interface ReadingPosition {
  filePath: string;
  // 1-based, inclusive.
  startLine: number;
  endLine: number;
}

export interface ReadAloud {
  // Called by the chat as each clip starts playing, and with undefined when reading stops.
  highlight(position: ReadingPosition | undefined): void;
}

// Two ways into the repo beyond the changes under review: asking the reviewer about
// any file (explore_reply), and reading a markdown file aloud (speak_file) with the
// passage being read highlighted in the editor.
export function register(
  backend: Backend,
  target: ChatTarget,
): { readAloud: ReadAloud; disposables: vscode.Disposable[] } {
  const decoration = vscode.window.createTextEditorDecorationType({
    isWholeLine: true,
    backgroundColor: new vscode.ThemeColor("editor.findMatchHighlightBackground"),
    overviewRulerColor: new vscode.ThemeColor("editor.findMatchHighlightBackground"),
    overviewRulerLane: vscode.OverviewRulerLane.Center,
  });
  let reading: ReadingPosition | undefined;
  publish("files.reading", () => reading);

  const decorate = (editor: vscode.TextEditor, reveal: boolean): void => {
    const root = backend.repoPath;
    const location = root ? reviewLocation(editor.document.uri, root) : undefined;
    if (!reading || location?.side !== "new" || location.filePath !== reading.filePath) {
      editor.setDecorations(decoration, []);
      return;
    }
    const range = new vscode.Range(reading.startLine - 1, 0, reading.endLine - 1, 0);
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
  let openedFor: string | undefined;
  const ensureTextVisible = async (filePath: string): Promise<void> => {
    const root = backend.repoPath;
    if (!root || openedFor === filePath) return;
    const shown = vscode.window.visibleTextEditors.some(
      (e) => reviewLocation(e.document.uri, root)?.filePath === filePath && e.document.uri.scheme === "file",
    );
    openedFor = filePath;
    if (shown) return;
    await vscode.window.showTextDocument(vscode.Uri.file(path.join(root, filePath)), {
      viewColumn: vscode.ViewColumn.Beside,
      preserveFocus: true,
      preview: true,
    });
  };

  const readAloud: ReadAloud = {
    highlight(position) {
      reading = position;
      if (!position) {
        openedFor = undefined;
        for (const editor of vscode.window.visibleTextEditors) decorate(editor, false);
        return;
      }
      void ensureTextVisible(position.filePath).then(() => {
        for (const editor of vscode.window.visibleTextEditors) decorate(editor, true);
      });
    },
  };

  // The file a command was invoked on: the Explorer passes its URI; from the Command
  // Palette it's the active editor's.
  // A markdown preview passes nothing and has no text editor; its tab is labelled
  // "Preview <file name>", so the document is the open markdown file of that name.
  // A preview opened straight from the Explorer may have no text document open, so
  // the name is looked for in open documents, then open tabs, then the workspace.
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

  const readFileAloud = async (arg: unknown): Promise<void> => {
    const uri = await targetUri(arg);
    if (!uri || uri.scheme !== "file" || !uri.fsPath.toLowerCase().endsWith(".md")) {
      showError("Read Aloud works on markdown (.md) files.");
      return;
    }
    const filePath = await ensureBackendFor(uri);
    if (!filePath) return;
    // A selection in that file reads just the blocks it touches.
    const editor = vscode.window.activeTextEditor;
    const selected =
      editor && editor.document.uri.toString() === uri.toString() && !editor.selection.isEmpty
        ? lineSpan(editor.selection)
        : undefined;
    backend.send(
      "speak_file",
      selected
        ? { file_path: filePath, start_line: selected.startLine, end_line: selected.endLine }
        : { file_path: filePath },
    );
    await vscode.commands.executeCommand("pearReview.chat.focus");
  };

  const run = (fn: (arg: unknown) => Promise<void>) => (arg: unknown) =>
    fn(arg).catch((err: unknown) => showError(err instanceof Error ? err.message : String(err)));

  return {
    readAloud,
    disposables: [
      decoration,
      vscode.commands.registerCommand("pearReview.askAboutFile", run(askAboutFile)),
      vscode.commands.registerCommand("pearReview.readAloud", run(readFileAloud)),
      vscode.commands.registerCommand("pearReview.openRepoFile", (filePath: unknown) => {
        if (typeof filePath === "string" && backend.repoPath) {
          void vscode.window.showTextDocument(vscode.Uri.file(path.join(backend.repoPath, filePath)));
        }
      }),
      // Decorations belong to an editor instance; reopening the file makes a new one.
      vscode.window.onDidChangeVisibleTextEditors((editors) => editors.forEach((e) => decorate(e, false))),
      backend.onStateChange((state) => {
        if (state !== "ready") readAloud.highlight(undefined);
      }),
    ],
  };
}
