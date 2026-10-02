import { randomBytes } from "node:crypto";
import * as vscode from "vscode";

import type { Backend, BackendState } from "../backend/backend.ts";
import type { ServerMessage } from "../backend/protocol.ts";
import { showError } from "../log.ts";
import type { Voice } from "./voice.ts";

// Extension → webview. media/chat/chat.js handles exactly these kinds.
type ToWebview =
  | { kind: "server"; message: ServerMessage }
  | { kind: "backend"; state: BackendState }
  | { kind: "recording"; recording: boolean };

// Webview → extension. Validated in parseFromWebview — the webview is a separate
// context, so its messages are checked like any other input.
type FromWebview =
  | { kind: "ready" }
  | { kind: "send"; text: string }
  | { kind: "command"; command: "explain" | "next" | "prev" | "startReview" | "toggleRecording" };

const COMMANDS = new Set(["explain", "next", "prev", "startReview", "toggleRecording"]);

// Server messages the chat shows today; the rest arrive as their UI is built.
const FORWARDED = new Set<ServerMessage["type"]>([
  "presenting",
  "narration",
  "human_turn",
  "reviewer_turn",
  "audio_chunk",
  "turn_audio_chunk",
  "notice",
  "error",
  "context_too_large",
]);

export function register(context: vscode.ExtensionContext, backend: Backend, voice: Voice): vscode.Disposable[] {
  const provider = new ChatViewProvider(context.extensionUri, backend, voice);
  return [vscode.window.registerWebviewViewProvider("pearReview.chat", provider), provider];
}

class ChatViewProvider implements vscode.WebviewViewProvider, vscode.Disposable {
  private view: vscode.WebviewView | undefined;
  private readonly subscriptions: vscode.Disposable[] = [];

  constructor(
    private readonly extensionUri: vscode.Uri,
    private readonly backend: Backend,
    voice: Voice,
  ) {
    this.subscriptions.push(
      backend.onStateChange((state) => this.post({ kind: "backend", state })),
      voice.onDidChange((recording) => this.post({ kind: "recording", recording })),
    );
    for (const type of FORWARDED) {
      this.subscriptions.push(
        backend.on(type, (payload) => this.post({ kind: "server", message: { type, payload } as ServerMessage })),
      );
    }
  }

  resolveWebviewView(view: vscode.WebviewView): void {
    this.view = view;
    const media = vscode.Uri.joinPath(this.extensionUri, "media", "chat");
    view.webview.options = { enableScripts: true, localResourceRoots: [media] };
    view.webview.html = this.html(view.webview, media);
    view.webview.onDidReceiveMessage((raw: unknown) => this.receive(raw), undefined, this.subscriptions);
    view.onDidDispose(() => (this.view = undefined), undefined, this.subscriptions);
  }

  dispose(): void {
    for (const d of this.subscriptions) d.dispose();
  }

  private post(message: ToWebview): void {
    void this.view?.webview.postMessage(message);
  }

  private receive(raw: unknown): void {
    const message = parseFromWebview(raw);
    if (!message) return;
    switch (message.kind) {
      case "ready":
        this.post({ kind: "backend", state: this.backend.state });
        return;
      case "send":
        try {
          this.backend.send("reply", { text: message.text });
        } catch (err) {
          showError(err instanceof Error ? err.message : String(err));
        }
        return;
      case "command":
        void vscode.commands.executeCommand(`pearReview.${message.command}`);
        return;
    }
  }

  private html(webview: vscode.Webview, media: vscode.Uri): string {
    const nonce = randomBytes(16).toString("base64");
    const script = webview.asWebviewUri(vscode.Uri.joinPath(media, "chat.js"));
    const style = webview.asWebviewUri(vscode.Uri.joinPath(media, "chat.css"));
    const csp = [
      "default-src 'none'",
      `style-src ${webview.cspSource}`,
      `script-src 'nonce-${nonce}'`,
      "media-src blob:",
    ].join("; ");
    return `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta http-equiv="Content-Security-Policy" content="${csp}">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="stylesheet" href="${style.toString()}">
  <title>Pear Review</title>
</head>
<body>
  <header id="hunk">No review running.</header>
  <div id="toolbar">
    <button data-command="startReview">Start review</button>
    <button data-command="prev" title="Previous hunk">Prev</button>
    <button data-command="next" title="Next hunk">Next</button>
    <button data-command="explain">Explain</button>
  </div>
  <main id="transcript" aria-live="polite"></main>
  <div id="audio-blocked" hidden><button id="enable-audio">Click to enable spoken replies</button></div>
  <form id="composer">
    <button type="button" id="mic" data-command="toggleRecording" title="Push to talk (Ctrl+Alt+Space)">Mic</button>
    <textarea id="input" rows="2" placeholder="Ask about this change…"></textarea>
    <button type="submit">Send</button>
  </form>
  <script nonce="${nonce}" src="${script.toString()}"></script>
</body>
</html>`;
  }
}

function parseFromWebview(raw: unknown): FromWebview | undefined {
  if (typeof raw !== "object" || raw === null) return undefined;
  const { kind, text, command } = raw as { kind?: unknown; text?: unknown; command?: unknown };
  if (kind === "ready") return { kind };
  if (kind === "send" && typeof text === "string" && text.trim()) return { kind, text: text.trim() };
  if (kind === "command" && typeof command === "string" && COMMANDS.has(command)) {
    return { kind, command: command as Extract<FromWebview, { kind: "command" }>["command"] };
  }
  return undefined;
}
