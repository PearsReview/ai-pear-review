import { randomBytes } from "node:crypto";
import * as vscode from "vscode";

import type { Backend, BackendState } from "../backend/backend.ts";
import type { ServerMessage } from "../backend/protocol.ts";
import { showError } from "../log.ts";
import type { SelectionContext } from "./selection.ts";
import type { Voice } from "./voice.ts";

// Extension → webview. media/chat/chat.js handles exactly these kinds.
type ToWebview =
  | { kind: "server"; message: ServerMessage }
  | { kind: "backend"; state: BackendState }
  | { kind: "recording"; recording: boolean }
  | { kind: "context"; label: string | null };

// Webview → extension. Validated in parseFromWebview: the webview is a separate
// context, so its messages are checked like any other input.
type FromWebview =
  | { kind: "ready" }
  | { kind: "send"; text: string }
  | { kind: "speak"; text: string }
  | { kind: "lookDeeper"; index: number; question?: string }
  | { kind: "jump"; index: number }
  | { kind: "clearContext" }
  | { kind: "command"; command: ChatCommand };

const COMMANDS = ["explain", "next", "prev", "startReview", "toggleRecording", "interrupt"] as const;
type ChatCommand = (typeof COMMANDS)[number];

// Server messages the chat shows; the rest arrive as their UI is built.
const FORWARDED = new Set<ServerMessage["type"]>([
  "presenting",
  "narration",
  "human_turn",
  "reviewer_turn",
  "deeper_turn",
  "agent_stopped",
  "audio_chunk",
  "turn_audio_chunk",
  "service_status",
  "notice",
  "error",
  "context_too_large",
]);

export function register(
  context: vscode.ExtensionContext,
  backend: Backend,
  voice: Voice,
  selection: SelectionContext,
): vscode.Disposable[] {
  const provider = new ChatViewProvider(context.extensionUri, backend, voice, selection);
  return [
    vscode.window.registerWebviewViewProvider("pearReview.chat", provider, {
      // Keeps the transcript when the view is hidden; it lives only in the webview.
      webviewOptions: { retainContextWhenHidden: true },
    }),
    provider,
  ];
}

class ChatViewProvider implements vscode.WebviewViewProvider, vscode.Disposable {
  private view: vscode.WebviewView | undefined;
  private readonly subscriptions: vscode.Disposable[] = [];

  constructor(
    private readonly extensionUri: vscode.Uri,
    private readonly backend: Backend,
    voice: Voice,
    private readonly selection: SelectionContext,
  ) {
    this.subscriptions.push(
      backend.onStateChange((state) => this.post({ kind: "backend", state })),
      voice.onDidChange((recording) => this.post({ kind: "recording", recording })),
      selection.onDidChange((label) => this.post({ kind: "context", label: label ?? null })),
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
    try {
      switch (message.kind) {
        case "ready":
          this.post({ kind: "backend", state: this.backend.state });
          this.post({ kind: "context", label: this.selection.label ?? null });
          return;
        case "send": {
          const marked_lines = this.selection.markedLines();
          this.backend.send("reply", marked_lines ? { text: message.text, marked_lines } : { text: message.text });
          // Context goes with one question, as the browser's markers do.
          this.selection.clear();
          return;
        }
        case "speak":
          this.backend.send("speak_turn", { text: message.text });
          return;
        case "lookDeeper":
          this.backend.send(
            "look_deeper",
            message.question ? { index: message.index, question: message.question } : { index: message.index },
          );
          return;
        case "jump":
          this.backend.send("jump_to_hunk", { index: message.index });
          return;
        case "clearContext":
          this.selection.clear();
          return;
        case "command":
          void vscode.commands.executeCommand(`pearReview.${message.command}`);
          return;
      }
    } catch (err) {
      showError(err instanceof Error ? err.message : String(err));
    }
  }

  private html(webview: vscode.Webview, media: vscode.Uri): string {
    const nonce = randomBytes(16).toString("base64");
    const src = (file: string): string => webview.asWebviewUri(vscode.Uri.joinPath(media, file)).toString();
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
  <link rel="stylesheet" href="${src("chat.css")}">
  <title>Pear Review</title>
</head>
<body>
  <header id="hunk">No review running.</header>
  <div id="toolbar">
    <button data-command="startReview" id="start">Start review</button>
    <button data-command="prev" class="secondary" title="Previous change">Prev</button>
    <button data-command="next" class="secondary" title="Next change">Next</button>
    <button data-command="explain" id="explain" class="secondary" title="Have the reviewer explain this change">Explain</button>
  </div>
  <main id="transcript" aria-live="polite"></main>
  <div id="audio-blocked" hidden><button id="enable-audio">Click to enable spoken replies</button></div>
  <div id="audio-bar" hidden>
    <span id="audio-status">Speaking…</span>
    <button id="audio-pause" class="secondary small">Pause</button>
    <button id="audio-stop" class="secondary small">Stop</button>
  </div>
  <div id="context" hidden>
    <span id="context-label"></span>
    <button id="context-clear" class="icon" title="Don't send this selection" aria-label="Don't send this selection">×</button>
  </div>
  <form id="composer">
    <button type="button" id="mic" data-command="toggleRecording" title="Push to talk (Ctrl+Alt+Space)">Mic</button>
    <textarea id="input" rows="2" placeholder="Ask about this change… (select lines in the editor to ask about them)"></textarea>
    <button type="submit" id="send">Send</button>
  </form>
  <script nonce="${nonce}" src="${src("blocks.js")}"></script>
  <script nonce="${nonce}" src="${src("chat.js")}"></script>
</body>
</html>`;
  }
}

function parseFromWebview(raw: unknown): FromWebview | undefined {
  if (typeof raw !== "object" || raw === null) return undefined;
  const m = raw as Record<string, unknown>;
  const text = typeof m.text === "string" ? m.text.trim() : "";
  const index = typeof m.index === "number" && Number.isInteger(m.index) && m.index >= 0 ? m.index : undefined;
  switch (m.kind) {
    case "ready":
    case "clearContext":
      return { kind: m.kind };
    case "send":
    case "speak":
      return text ? { kind: m.kind, text } : undefined;
    case "jump":
      return index === undefined ? undefined : { kind: "jump", index };
    case "lookDeeper": {
      if (index === undefined) return undefined;
      const question = typeof m.question === "string" && m.question.trim() ? m.question.trim() : undefined;
      return question ? { kind: "lookDeeper", index, question } : { kind: "lookDeeper", index };
    }
    case "command":
      return (COMMANDS as readonly unknown[]).includes(m.command)
        ? { kind: "command", command: m.command as ChatCommand }
        : undefined;
    default:
      return undefined;
  }
}
