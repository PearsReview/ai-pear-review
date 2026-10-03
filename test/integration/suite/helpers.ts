// Shared by the integration tests: the extension's test probe, the fakes' control
// endpoints, and waiting for asynchronous UI state to settle.
import * as path from "node:path";
import * as vscode from "vscode";

export const live = process.env.PEAR_TEST_LIVE_MODEL === "1";
export const repo = process.env.PEAR_TEST_REPO ?? "";
const fakeUrl = process.env.PEAR_TEST_FAKE_URL ?? "";
const WAIT_MS = live ? 300_000 : 20_000;

interface Api {
  read(name: string): unknown;
}

let api: Api | undefined;

export async function probe<T>(name: string): Promise<T> {
  if (!api) {
    const extension = vscode.extensions.getExtension<Api>("PearsReview.ai-pear-review");
    if (!extension) throw new Error("The extension under test isn't loaded.");
    api = await extension.activate();
    if (!api) throw new Error("The extension returned no test API; is PEAR_REVIEW_TEST=1 set?");
  }
  return api.read(name) as T;
}

export async function waitFor<T>(
  what: string,
  check: () => T | undefined | false | Promise<T | undefined | false>,
  timeoutMs = WAIT_MS,
): Promise<T> {
  const deadline = Date.now() + timeoutMs;
  let last: unknown;
  while (Date.now() < deadline) {
    try {
      const value: T | undefined | false = await check();
      if (value) return value;
    } catch (err) {
      last = err;
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  const reason = last instanceof Error ? `: ${last.message}` : "";
  throw new Error(`Timed out waiting for ${what}${reason}`);
}

// --- the chat panel -------------------------------------------------------------------

export interface Posted {
  kind: string;
  message?: { type: string; payload: Record<string, unknown> };
  [key: string]: unknown;
}

export async function posted(): Promise<Posted[]> {
  return probe<Posted[]>("chat.posted");
}

// Server messages forwarded to the chat since `from` (an index into posted()).
export async function serverMessages(type: string, from = 0): Promise<Record<string, unknown>[]> {
  return (await posted())
    .slice(from)
    .filter((m) => m.kind === "server" && m.message?.type === type)
    .map((m) => m.message?.payload ?? {});
}

export async function nextServerMessage(
  type: string,
  from: number,
  where: (payload: Record<string, unknown>) => boolean = () => true,
): Promise<Record<string, unknown>> {
  return waitFor(`a "${type}" message in the chat`, async () => (await serverMessages(type, from)).find(where));
}

export async function mark(): Promise<number> {
  return (await posted()).length;
}

// What the chat webview would send when the reviewer clicks or types.
export async function fromChat(message: Record<string, unknown>): Promise<void> {
  const receive = await probe<(raw: unknown) => void>("chat.receive");
  receive(message);
}

// --- the fakes ---------------------------------------------------------------------

export async function fakeRequests(): Promise<{ path: string; body: Record<string, unknown> }[]> {
  return (await fetch(`${fakeUrl}/__requests`)).json() as Promise<{ path: string; body: Record<string, unknown> }[]>;
}

export async function scriptFake(script: { replies?: string[]; transcript?: string }): Promise<void> {
  await fetch(`${fakeUrl}/__script`, { method: "POST", body: JSON.stringify(script) });
}

// The text of every user message the fake model has been sent.
export async function modelPrompts(): Promise<string> {
  const chats = (await fakeRequests()).filter((r) => r.path === "/api/chat");
  return chats
    .flatMap((r) => (r.body.messages as { role: string; content: string }[] | undefined) ?? [])
    .filter((m) => m.role === "user")
    .map((m) => m.content)
    .join("\n---\n");
}

// --- the review ----------------------------------------------------------------------

export const repoUri = (file: string): vscode.Uri => vscode.Uri.file(path.join(repo, file));

export async function ensureReviewStarted(): Promise<void> {
  const tree = await probe<{ files: unknown[] }>("tree.view");
  if (tree.files.length) return;
  await vscode.commands.executeCommand("pearReview.startReview");
  // The first start imports the backend and runs its preflight: slower than any later step.
  await waitFor(
    "the Changes tree to fill",
    async () => (await probe<{ files: unknown[] }>("tree.view")).files.length > 0,
    Math.max(WAIT_MS, 90_000),
  );
}

// Selects lines in an editor and waits until the chat shows them as the next question's
// context: the selection reaches the extension asynchronously.
export async function selectLines(file: string, startLine: number, endLine: number): Promise<vscode.TextEditor> {
  const editor = await vscode.window.showTextDocument(repoUri(file), { preview: false });
  editor.selection = new vscode.Selection(
    startLine - 1,
    0,
    endLine - 1,
    editor.document.lineAt(endLine - 1).text.length,
  );
  const name = file.split("/").pop() ?? file;
  const label = startLine === endLine ? `${name}, line ${startLine}` : `${name}, lines ${startLine}–${endLine}`;
  await waitFor(`the chat to show "${label}"`, async () => {
    const last = [...(await posted())].reverse().find((m) => m.kind === "context");
    return last?.label === label;
  });
  return editor;
}

export function clearSelection(): void {
  const editor = vscode.window.activeTextEditor;
  if (editor) editor.selection = new vscode.Selection(editor.selection.active, editor.selection.active);
}
