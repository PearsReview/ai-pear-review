import { randomBytes } from "node:crypto";
import * as vscode from "vscode";

import type { Backend, BackendState } from "../backend/backend.ts";
import type { ServerMessage } from "../backend/protocol.ts";
import { showError } from "../log.ts";
import { publish, testMode } from "../testProbe.ts";
import type { ActNow } from "./actNow.ts";
import { chatHtml } from "./chatHtml.ts";
import type { Comments } from "./comments.ts";
import type { ReadAloud } from "./files.ts";
import type { Prefs, PrefValues } from "./prefs.ts";
import type { SelectionContext } from "./selection.ts";
import type { ChatTarget } from "./target.ts";
import type { Voice } from "./voice.ts";

// Extension → webview. media/chat/chat.js handles exactly these kinds.
type ToWebview =
  | { kind: "server"; message: ServerMessage }
  | { kind: "backend"; state: BackendState }
  | { kind: "recording"; recording: boolean }
  | { kind: "context"; label: string | null }
  | { kind: "actMode"; on: boolean }
  | { kind: "commentMode"; on: boolean }
  | { kind: "target"; file_path: string | null }
  | { kind: "prefs"; prefs: PrefValues }
  // An Act Now proposal, without its diffs (those open as editor tabs).
  | { kind: "proposal"; agent: string; summary: string; files: { file_path: string; status: string }[] };

// Webview → extension. Validated in parseFromWebview: the webview is a separate
// context, so its messages are checked like any other input.
type FromWebview =
  | { kind: "ready" }
  | { kind: "send"; text: string }
  | { kind: "speak"; text: string }
  | { kind: "speakFile"; file_path: string }
  | { kind: "lookDeeper"; index: number; question?: string }
  | { kind: "jump"; index: number }
  | { kind: "clearContext" }
  | { kind: "actNow"; text: string }
  | { kind: "setActMode"; on: boolean }
  | { kind: "comment"; text: string }
  | { kind: "setCommentMode"; on: boolean }
  | { kind: "openPlan"; file: string }
  | { kind: "proposal"; action: "apply" | "discard" }
  | { kind: "proposal"; action: "refine"; text: string }
  | { kind: "proposal"; action: "open"; file_path: string }
  | { kind: "backToReview" }
  | { kind: "openFile"; file_path: string }
  | { kind: "reading"; file_path: string; start_line: number; end_line: number }
  | { kind: "readingDone" }
  | { kind: "command"; command: ChatCommand };

const COMMANDS = ["explain", "toggleRecording", "interrupt", "settings", "createPlan", "newReview"] as const;
type ChatCommand = (typeof COMMANDS)[number];

// Server messages the chat shows; the rest arrive as their UI is built.
const FORWARDED = new Set<ServerMessage["type"]>([
  "presenting",
  "narration",
  "human_turn",
  "reviewer_turn",
  "deeper_turn",
  "agent_stopped",
  "act_now_cleared",
  "review_comment_queued",
  "audio_chunk",
  "turn_audio_chunk",
  "file_audio_chunk",
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
  actNow: ActNow,
  target: ChatTarget,
  readAloud: ReadAloud,
  prefs: Prefs,
  comments: Comments,
): vscode.Disposable[] {
  const provider = new ChatViewProvider(
    context.extensionUri,
    backend,
    voice,
    selection,
    actNow,
    target,
    readAloud,
    prefs,
    comments,
  );
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
  // Everything posted to the webview, for the integration tests (testProbe.ts).
  private readonly posted: ToWebview[] = [];

  constructor(
    private readonly extensionUri: vscode.Uri,
    private readonly backend: Backend,
    voice: Voice,
    private readonly selection: SelectionContext,
    private readonly actNow: ActNow,
    private readonly target: ChatTarget,
    private readonly readAloud: ReadAloud,
    private readonly prefs: Prefs,
    private readonly comments: Comments,
  ) {
    this.subscriptions.push(comments.onDidChangeCommentMode((on) => this.post({ kind: "commentMode", on })));
    this.subscriptions.push(prefs.onDidChange((values) => this.post({ kind: "prefs", prefs: values })));
    publish("chat.posted", () => this.posted);
    publish("chat.receive", () => (raw: unknown) => this.receive(raw));
    this.subscriptions.push(
      target.onDidChange((file) => this.post({ kind: "target", file_path: file ?? null })),
      actNow.onDidChangeActive((on) => this.post({ kind: "actMode", on })),
      backend.on("act_now_preview", (p) =>
        this.post({
          kind: "proposal",
          agent: p.agent,
          summary: p.summary,
          files: p.files.map((f) => ({ file_path: f.file_path, status: f.status })),
        }),
      ),
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
    // Like the Changes tree, showing the chat opens the changes.
    if (this.backend.state === "stopped") {
      void vscode.commands.executeCommand("pearReview.openChanges", { quiet: true });
    }
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
    if (testMode) this.posted.push(message);
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
          this.post({ kind: "actMode", on: this.actNow.active });
          this.post({ kind: "target", file_path: this.target.file ?? null });
          this.post({ kind: "prefs", prefs: this.prefs.values });
          this.post({ kind: "commentMode", on: this.comments.commentMode });
          return;
        case "send": {
          const marked_lines = this.selection.markedLines();
          const extra = marked_lines ? { marked_lines } : {};
          const file = this.target.file;
          if (file) this.backend.send("explore_reply", { text: message.text, file_path: file, ...extra });
          else this.backend.send("reply", { text: message.text, ...extra });
          // Context goes with one question, as the browser's markers do.
          this.selection.clear();
          return;
        }
        case "speak":
          this.backend.send("speak_turn", { text: message.text });
          return;
        case "speakFile":
          this.backend.send("speak_file", { file_path: message.file_path });
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
        case "actNow":
          this.actNow.request({ text: message.text }, this.selection.markedLines());
          this.selection.clear();
          return;
        case "setActMode":
          // Act and Comment both claim the next message, so only one is on.
          if (message.on) this.comments.setCommentMode(false);
          this.actNow.setActive(message.on);
          return;
        case "setCommentMode":
          if (message.on) this.actNow.setActive(false);
          this.comments.setCommentMode(message.on);
          return;
        case "comment":
          this.comments.request({ text: message.text }, this.selection.markedLines());
          this.selection.clear();
          return;
        case "openPlan":
          void vscode.commands.executeCommand("pearReview.openPlan", message.file);
          return;
        case "backToReview":
          this.target.setFile(undefined);
          return;
        case "openFile":
          void vscode.commands.executeCommand("pearReview.openRepoFile", message.file_path);
          return;
        case "reading":
          this.readAloud.highlight({
            filePath: message.file_path,
            startLine: message.start_line,
            endLine: message.end_line,
          });
          return;
        case "readingDone":
          this.readAloud.highlight(undefined);
          return;
        case "proposal":
          if (message.action === "open") {
            void vscode.commands.executeCommand("pearReview.actNow.openFile", message.file_path);
          } else if (message.action === "refine") {
            void vscode.commands.executeCommand("pearReview.actNow.refine", message.text);
          } else {
            void vscode.commands.executeCommand(`pearReview.actNow.${message.action}`);
          }
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
    return chatHtml({
      nonce: randomBytes(16).toString("base64"),
      cspSource: webview.cspSource,
      src: (file) => webview.asWebviewUri(vscode.Uri.joinPath(media, file)).toString(),
    });
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
    case "backToReview":
    case "readingDone":
      return { kind: m.kind };
    case "openFile":
    case "speakFile":
      return typeof m.file_path === "string" ? { kind: m.kind, file_path: m.file_path } : undefined;
    case "reading": {
      const { file_path, start_line, end_line } = m;
      return typeof file_path === "string" && typeof start_line === "number" && typeof end_line === "number"
        ? { kind: "reading", file_path, start_line, end_line }
        : undefined;
    }
    case "send":
    case "speak":
    case "actNow":
    case "comment":
      return text ? { kind: m.kind, text } : undefined;
    case "setCommentMode":
      return typeof m.on === "boolean" ? { kind: "setCommentMode", on: m.on } : undefined;
    case "openPlan":
      return typeof m.file === "string" ? { kind: "openPlan", file: m.file } : undefined;
    case "setActMode":
      return typeof m.on === "boolean" ? { kind: "setActMode", on: m.on } : undefined;
    case "proposal":
      if (m.action === "apply" || m.action === "discard") return { kind: "proposal", action: m.action };
      if (m.action === "refine") return text ? { kind: "proposal", action: "refine", text } : undefined;
      if (m.action === "open" && typeof m.file_path === "string") {
        return { kind: "proposal", action: "open", file_path: m.file_path };
      }
      return undefined;
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
