import * as vscode from "vscode";

import { PythonBackend } from "./backend/backend.ts";
import { output } from "./log.ts";
import * as actNow from "./ui/actNow.ts";
import * as chatPanel from "./ui/chatPanel.ts";
import * as commands from "./ui/commands.ts";
import * as comments from "./ui/comments.ts";
import * as diffView from "./ui/diffView.ts";
import * as hunkTree from "./ui/hunkTree.ts";
import * as selectionContext from "./ui/selection.ts";
import * as settings from "./ui/settings.ts";
import * as statusBar from "./ui/statusBar.ts";
import * as voice from "./ui/voice.ts";

let backend: PythonBackend | undefined;

// Wiring only. Nothing here blocks or spawns: the backend starts on the first
// "Start Review".
export function activate(context: vscode.ExtensionContext): void {
  backend = new PythonBackend(context.extensionPath, context.secrets);
  const selection = selectionContext.register(backend);
  const agent = actNow.register(backend);
  const recorder = voice.register(backend, selection.selection, agent.actNow);
  const review = comments.register(backend, selection.selection);
  context.subscriptions.push(
    output,
    backend,
    ...statusBar.register(backend),
    ...review.disposables,
    ...hunkTree.register(backend, review.comments),
    ...diffView.register(backend),
    ...selection.disposables,
    ...agent.disposables,
    ...settings.register(backend),
    ...recorder.disposables,
    ...chatPanel.register(context, backend, recorder.voice, selection.selection, agent.actNow),
    ...commands.register(context, backend, recorder.voice),
  );
}

export async function deactivate(): Promise<void> {
  await backend?.stop();
}
