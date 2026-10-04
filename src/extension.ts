import * as vscode from "vscode";

import { PythonBackend } from "./backend/backend.ts";
import { output } from "./log.ts";
import { read, testMode } from "./testProbe.ts";
import * as actNow from "./ui/actNow.ts";
import * as chatPanel from "./ui/chatPanel.ts";
import * as commands from "./ui/commands.ts";
import * as comments from "./ui/comments.ts";
import * as diffView from "./ui/diffView.ts";
import * as files from "./ui/files.ts";
import * as hunkTree from "./ui/hunkTree.ts";
import * as preferences from "./ui/prefs.ts";
import * as selectionContext from "./ui/selection.ts";
import * as settings from "./ui/settings.ts";
import * as statusBar from "./ui/statusBar.ts";
import * as chatTarget from "./ui/target.ts";
import * as voice from "./ui/voice.ts";

let backend: PythonBackend | undefined;

// Wiring only. Nothing here blocks or spawns: the backend starts on the first
// "Start Review".
// Returns the test probe's reader when PEAR_REVIEW_TEST=1, and nothing otherwise.
export function activate(context: vscode.ExtensionContext): { read: typeof read } | undefined {
  backend = new PythonBackend(context.extensionPath, context.secrets);
  const prefs = preferences.register(context, backend);
  const selection = selectionContext.register(backend);
  const agent = actNow.register(backend);
  const target = chatTarget.register(backend);
  const repoFiles = files.register(backend, target.target);
  const review = comments.register(backend, selection.selection);
  const recorder = voice.register(backend, selection.selection, agent.actNow, target.target, review.comments);
  context.subscriptions.push(
    output,
    backend,
    ...statusBar.register(backend),
    ...review.disposables,
    ...hunkTree.register(backend, review.comments),
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
      repoFiles.readAloud,
      prefs.prefs,
    ),
    ...commands.register(context, backend, recorder.voice, prefs.prefs),
  );
  return testMode ? { read } : undefined;
}

export async function deactivate(): Promise<void> {
  await backend?.stop();
}
