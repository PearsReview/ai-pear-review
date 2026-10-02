import * as vscode from "vscode";

import { PythonBackend } from "./backend/backend.ts";
import { output } from "./log.ts";
import * as chatPanel from "./ui/chatPanel.ts";
import * as commands from "./ui/commands.ts";
import * as statusBar from "./ui/statusBar.ts";
import * as voice from "./ui/voice.ts";

let backend: PythonBackend | undefined;

// Wiring only. Nothing here blocks or spawns: the backend starts on the first
// "Start Review".
export function activate(context: vscode.ExtensionContext): void {
  backend = new PythonBackend(context.extensionPath, context.secrets);
  const recorder = voice.register(backend);
  context.subscriptions.push(
    output,
    backend,
    ...statusBar.register(backend),
    ...recorder.disposables,
    ...chatPanel.register(context, backend, recorder.voice),
    ...commands.register(context, backend, recorder.voice),
  );
}

export async function deactivate(): Promise<void> {
  await backend?.stop();
}
