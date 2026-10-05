import * as path from "node:path";
import * as vscode from "vscode";

import { Speaking } from "./audio/speaking.ts";
import { BackendManager } from "./backend/manager.ts";
import { setStorageDir } from "./backend/python.ts";
import { output } from "./log.ts";
import { readingPlugin, type MarkdownItLike } from "./review/markdownReading.ts";
import { read, testMode } from "./testProbe.ts";
import * as actNow from "./ui/actNow.ts";
import * as chatPanel from "./ui/chatPanel.ts";
import * as commands from "./ui/commands.ts";
import * as comments from "./ui/comments.ts";
import * as diffView from "./ui/diffView.ts";
import * as hunkTree from "./ui/hunkTree.ts";
import * as notices from "./ui/notices.ts";
import * as preferences from "./ui/prefs.ts";
import * as pullRequests from "./ui/pullRequests.ts";
import * as pythonSetup from "./ui/pythonSetup.ts";
import * as readAloud from "./ui/readAloud.ts";
import * as readingHighlight from "./ui/readingHighlight.ts";
import * as repoFiles from "./ui/repoFiles.ts";
import * as repositories from "./ui/repositories.ts";
import * as selectionContext from "./ui/selection.ts";
import * as settings from "./ui/settings.ts";
import * as statusBar from "./ui/statusBar.ts";
import * as chatTarget from "./ui/target.ts";
import * as voice from "./ui/voice.ts";

let backend: BackendManager | undefined;

// Wiring only. Nothing here blocks or spawns: a repository's backend starts when the
// Pear Review view or chat first shows, or on Start Review.
// Returns the markdown preview's plugin (contributes."markdown.markdownItPlugins": VS Code
// asks for it when a preview opens), and the test probe's reader when PEAR_REVIEW_TEST=1.
export function activate(context: vscode.ExtensionContext): {
  extendMarkdownIt(md: MarkdownItLike): MarkdownItLike;
  read?: typeof read;
} {
  setStorageDir(context.globalStorageUri.fsPath);
  backend = new BackendManager(context.extensionPath, context.secrets);
  const repos = repositories.register(backend);
  const prefs = preferences.register(context, backend);
  const selection = selectionContext.register(backend);
  const agent = actNow.register(backend);
  const target = chatTarget.register(backend);
  const speaking = new Speaking();
  const reading = readingHighlight.register(backend);
  const reader = readAloud.register(context, backend, reading.highlight, speaking);
  const review = comments.register(backend, selection.selection);
  const recorder = voice.register({
    backend,
    selection: selection.selection,
    actNow: agent.actNow,
    target: target.target,
    comments: review.comments,
  });
  context.subscriptions.push(
    output,
    backend,
    speaking,
    ...statusBar.register(backend),
    ...notices.register(backend),
    ...review.disposables,
    ...repos.disposables,
    ...pythonSetup.register(context),
    ...hunkTree.register(backend, review.comments, reader.reader, repos.repos),
    ...diffView.register(backend),
    ...selection.disposables,
    ...agent.disposables,
    ...target.disposables,
    ...reading.disposables,
    ...reader.disposables,
    ...repoFiles.register(backend, target.target),
    ...prefs.disposables,
    ...settings.register(context, backend, prefs.prefs),
    ...recorder.disposables,
    ...chatPanel.register(context, {
      backend,
      voice: recorder.voice,
      selection: selection.selection,
      actNow: agent.actNow,
      target: target.target,
      prefs: prefs.prefs,
      speaking,
    }),
    ...commands.register(context, backend, recorder.voice, prefs.prefs),
    ...pullRequests.register(context, backend, review.comments),
  );
  const samePath = (a: string, b: string): boolean =>
    process.platform === "win32"
      ? path.normalize(a).toLowerCase() === path.normalize(b).toLowerCase()
      : path.normalize(a) === path.normalize(b);
  return {
    extendMarkdownIt(md: MarkdownItLike): MarkdownItLike {
      readingPlugin(md, () => reader.reader.spot, samePath);
      return md;
    },
    ...(testMode ? { read } : {}),
  };
}

export async function deactivate(): Promise<void> {
  await backend?.stopAll();
}
