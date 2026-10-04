import * as path from "node:path";
import * as vscode from "vscode";

import { Speaking } from "./audio/speaking.ts";
import { BackendManager } from "./backend/manager.ts";
import { output } from "./log.ts";
import { readingPlugin, type MarkdownItLike } from "./review/markdownReading.ts";
import { read, testMode } from "./testProbe.ts";
import * as actNow from "./ui/actNow.ts";
import * as chatPanel from "./ui/chatPanel.ts";
import * as commands from "./ui/commands.ts";
import * as comments from "./ui/comments.ts";
import * as diffView from "./ui/diffView.ts";
import * as files from "./ui/files.ts";
import * as hunkTree from "./ui/hunkTree.ts";
import * as notices from "./ui/notices.ts";
import * as preferences from "./ui/prefs.ts";
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
  backend = new BackendManager(context.extensionPath, context.secrets);
  const repos = repositories.register(backend);
  const prefs = preferences.register(context, backend);
  const selection = selectionContext.register(backend);
  const agent = actNow.register(backend);
  const target = chatTarget.register(backend);
  const speaking = new Speaking();
  const repoFiles = files.register(context, backend, target.target, speaking);
  const review = comments.register(backend, selection.selection);
  const recorder = voice.register(backend, selection.selection, agent.actNow, target.target, review.comments);
  context.subscriptions.push(
    output,
    backend,
    speaking,
    ...statusBar.register(backend),
    ...notices.register(backend),
    ...review.disposables,
    ...repos.disposables,
    ...hunkTree.register(backend, review.comments, repoFiles.reader, repos.repos),
    ...diffView.register(backend),
    ...selection.disposables,
    ...agent.disposables,
    ...target.disposables,
    ...repoFiles.disposables,
    ...prefs.disposables,
    ...settings.register(context, backend, prefs.prefs),
    ...recorder.disposables,
    ...chatPanel.register(
      context,
      backend,
      recorder.voice,
      selection.selection,
      agent.actNow,
      target.target,
      prefs.prefs,
      speaking,
    ),
    ...commands.register(context, backend, recorder.voice, prefs.prefs),
  );
  const samePath = (a: string, b: string): boolean =>
    process.platform === "win32"
      ? path.normalize(a).toLowerCase() === path.normalize(b).toLowerCase()
      : path.normalize(a) === path.normalize(b);
  return {
    extendMarkdownIt(md: MarkdownItLike): MarkdownItLike {
      readingPlugin(md, () => repoFiles.reader.spot, samePath);
      return md;
    },
    ...(testMode ? { read } : {}),
  };
}

export async function deactivate(): Promise<void> {
  await backend?.stopAll();
}
