// The chat webview's own behaviour (media/chat/chat.js) in a DOM: what it renders for
// each message from the extension, and what it posts back when the reviewer clicks or
// types. The integration suite covers the extension side of the same messages.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { afterEach, beforeEach, describe, test } from "node:test";

import { JSDOM } from "jsdom";

import { chatHtml } from "../../src/ui/chatHtml.ts";

const media = (file: string): string => readFileSync(new URL(`../../media/chat/${file}`, import.meta.url), "utf8");
const scripts = media("blocks.js") + "\n" + media("chat.js");

// jsdom has no media playback; this stands in for HTMLAudioElement.
class FakeAudio {
  src = "";
  paused = true;
  ended = false;
  currentTime = 0;
  private listeners = new Map<string, (() => void)[]>();
  static last: FakeAudio | undefined;
  // Set to refuse play() once, as a panel does before its first click.
  static refuseNext = false;
  constructor() {
    FakeAudio.last = this;
  }
  play(): Promise<void> {
    if (FakeAudio.refuseNext) {
      FakeAudio.refuseNext = false;
      return Promise.reject(Object.assign(new Error("blocked"), { name: "NotAllowedError" }));
    }
    this.paused = false;
    this.currentTime = 0.1;
    return Promise.resolve();
  }
  pause(): void {
    this.paused = true;
  }
  addEventListener(type: string, fn: () => void): void {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), fn]);
  }
  finish(): void {
    this.paused = true;
    this.ended = true;
    for (const fn of this.listeners.get("ended") ?? []) fn();
  }
}

interface Chat {
  window: JSDOM["window"];
  doc: Document;
  posts: Record<string, unknown>[];
  send(message: Record<string, unknown>): void;
  server(type: string, payload: Record<string, unknown>): void;
  $(id: string): HTMLElement;
  click(id: string): void;
}

function openChat(): Chat {
  const dom = new JSDOM(chatHtml({ nonce: "n", cspSource: "self", src: (f) => f }), { runScripts: "outside-only" });
  const { window } = dom;
  const posts: Record<string, unknown>[] = [];
  Object.assign(window, {
    // Copied out of the jsdom realm, whose objects have their own prototypes and so
    // never deep-equal this realm's literals.
    acquireVsCodeApi: () => ({
      postMessage: (m: Record<string, unknown>) => posts.push(JSON.parse(JSON.stringify(m)) as Record<string, unknown>),
    }),
    Audio: FakeAudio,
  });
  window.URL.createObjectURL = () => "blob:fake";
  window.URL.revokeObjectURL = () => undefined;
  window.HTMLElement.prototype.scrollIntoView = () => undefined;
  window.eval(scripts);
  const send = (message: Record<string, unknown>): void => {
    window.dispatchEvent(new window.MessageEvent("message", { data: message }));
  };
  const $ = (id: string): HTMLElement => {
    const el = window.document.getElementById(id);
    assert.ok(el, `#${id} exists`);
    return el;
  };
  return {
    window,
    doc: window.document,
    posts,
    send,
    server: (type, payload) => send({ kind: "server", message: { type, payload } }),
    $,
    click: (id) => $(id).click(),
  };
}

const AUDIO = "UklGRg=="; // any base64; FakeAudio never decodes it
const hunk = { index: 0, total: 2, done: false, file_path: "calc.py", review_started: true, narration_available: true };
const narration = {
  text: "It calls `add`.",
  spoken: "It calls add.",
  index: 0,
  total: 2,
  file_path: "calc.py",
  narration_available: true,
  blocks: [
    {
      kind: "paragraph",
      level: 0,
      spans: [
        { text: "It calls ", style: "plain" },
        { text: "add", style: "code" },
        { text: ".", style: "plain" },
      ],
    },
  ],
};
const lastPost = (chat: Chat): Record<string, unknown> | undefined => chat.posts[chat.posts.length - 1];

void describe("chat webview", () => {
  let chat: Chat;
  beforeEach(() => {
    chat = openChat();
    chat.send({ kind: "backend", state: "ready" });
  });
  // Closing the window clears its timers (the Look deeper ticker), so the run can exit.
  afterEach(() => chat.window.close());

  void test("says it is ready when it loads", () => {
    assert.deepEqual(chat.posts[0], { kind: "ready" });
  });

  void test("shows the hunk and enables the composer once a hunk is presented", () => {
    assert.equal((chat.$("send") as HTMLButtonElement).disabled, true);
    chat.server("presenting", hunk);
    assert.match(chat.$("hunk").textContent ?? "", /calc\.py\s+·\s+change 1 of 2/);
    assert.equal((chat.$("send") as HTMLButtonElement).disabled, false);
  });

  void test("renders a narration's blocks under a divider, with a speaker and Look deeper", () => {
    chat.server("presenting", hunk);
    chat.server("narration", narration);
    const divider = chat.doc.querySelector(".hunk-divider");
    assert.match(divider?.textContent ?? "", /calc\.py — change 1\/2/);
    assert.equal(chat.doc.querySelector(".turn.presenter code.md-code")?.textContent, "add");
    const deeper = chat.doc.querySelector<HTMLButtonElement>(".look-deeper");
    assert.equal(deeper?.disabled, true, "Look deeper waits for a coding agent");
    chat.server("service_status", { act_now: { available: true, detail: "ok", agent: "Cline" } });
    assert.equal(deeper?.disabled, false);
    deeper?.click();
    assert.deepEqual(lastPost(chat), { kind: "lookDeeper", index: 0 });
    assert.ok(chat.doc.querySelector(".turn.deeper-pending"), "a pending bubble shows while the agent works");
  });

  void test("the divider jumps back to its hunk", () => {
    chat.server("presenting", hunk);
    chat.server("narration", narration);
    chat.doc.querySelector<HTMLButtonElement>(".hunk-divider")?.click();
    assert.deepEqual(lastPost(chat), { kind: "jump", index: 0 });
  });

  void test("the speaker button goes loading → pause → play → idle", () => {
    chat.server("presenting", hunk);
    chat.server("narration", narration);
    const speak = chat.doc.querySelector<HTMLButtonElement>("button.speak");
    assert.ok(speak);
    speak.click();
    assert.deepEqual(lastPost(chat), { kind: "speak", text: "It calls add." });
    assert.equal(speak.dataset.mode, "loading");
    assert.ok(speak.querySelector(".codicon-loading.codicon-modifier-spin"), "a spinner while speech is made");
    chat.server("turn_audio_chunk", { audio_base64: AUDIO, mime_type: "audio/wav", chunk_index: 0, chunk_count: 1 });
    assert.equal(speak.dataset.mode, "playing");
    assert.ok(speak.querySelector(".codicon-debug-pause"), "playing shows pause");
    speak.click();
    assert.equal(speak.dataset.mode, "paused");
    assert.equal(FakeAudio.last?.paused, true);
    assert.ok(speak.querySelector(".codicon-play"), "paused shows play");
    speak.click();
    assert.equal(speak.dataset.mode, "playing");
    assert.equal(FakeAudio.last?.paused, false);
    FakeAudio.last?.finish();
    assert.equal(speak.dataset.mode, "idle");
  });

  void test("clicking while speech is being made cancels it", () => {
    chat.server("presenting", hunk);
    chat.server("narration", narration);
    const speak = chat.doc.querySelector<HTMLButtonElement>("button.speak");
    speak?.click();
    speak?.click();
    assert.equal(speak?.dataset.mode, "idle");
    assert.deepEqual(lastPost(chat), { kind: "command", command: "interrupt" });
  });

  void test("narration speaks under its own message's button", () => {
    chat.server("presenting", hunk);
    chat.server("narration", narration);
    chat.server("audio_chunk", { audio_base64: AUDIO, mime_type: "audio/wav", chunk_index: 0, chunk_count: 1 });
    const speak = chat.doc.querySelector<HTMLButtonElement>("button.speak");
    assert.equal(speak?.dataset.mode, "playing");
    assert.equal(chat.doc.getElementById("audio-bar"), null, "there is no separate audio bar");
  });

  void test("audio the panel may not play yet waits on the message's Play", async () => {
    chat.server("presenting", hunk);
    chat.server("narration", narration);
    FakeAudio.refuseNext = true;
    chat.server("audio_chunk", { audio_base64: AUDIO, mime_type: "audio/wav", chunk_index: 0, chunk_count: 1 });
    await new Promise((resolve) => setTimeout(resolve, 0));
    const speak = chat.doc.querySelector<HTMLButtonElement>("button.speak");
    assert.equal(speak?.dataset.mode, "paused");
    speak?.click();
    assert.equal(FakeAudio.last?.paused, false, "the click starts playback");
    assert.equal(speak?.dataset.mode, "playing");
  });

  void test("sends a typed question on Enter", () => {
    chat.server("presenting", hunk);
    const input = chat.$("input") as HTMLTextAreaElement;
    input.value = "Why?";
    input.dispatchEvent(new chat.window.KeyboardEvent("keydown", { key: "Enter" }));
    assert.deepEqual(lastPost(chat), { kind: "send", text: "Why?" });
    assert.equal(input.value, "");
  });

  void test("act mode sends the next message to the agent", () => {
    chat.server("presenting", hunk);
    chat.server("service_status", { act_now: { available: true, detail: "ok", agent: "Cline" } });
    chat.click("act");
    assert.deepEqual(lastPost(chat), { kind: "setActMode", on: true });
    chat.send({ kind: "actMode", on: true });
    assert.match((chat.$("input") as HTMLTextAreaElement).placeholder, /Tell Cline what to change/);
    assert.equal(chat.$("send").getAttribute("aria-label"), "Send to agent");
    (chat.$("input") as HTMLTextAreaElement).value = "Add a guard";
    chat.$("composer").dispatchEvent(new chat.window.Event("submit", { cancelable: true }));
    assert.deepEqual(lastPost(chat), { kind: "actNow", text: "Add a guard" });
  });

  void test("a proposal card applies, and settles when the backend confirms", () => {
    chat.server("presenting", hunk);
    chat.send({
      kind: "proposal",
      agent: "Cline",
      summary: "Added a guard.",
      files: [{ file_path: "calc.py", status: "modified" }],
    });
    const card = chat.doc.querySelector(".turn.proposal");
    assert.ok(card);
    assert.match(card.textContent ?? "", /modified · calc\.py/);
    card.querySelector<HTMLButtonElement>(".proposal-files button")?.click();
    assert.deepEqual(lastPost(chat), { kind: "proposal", action: "open", file_path: "calc.py" });
    [...card.querySelectorAll<HTMLButtonElement>("button")].find((b) => b.textContent === "Apply")?.click();
    assert.deepEqual(lastPost(chat), { kind: "proposal", action: "apply" });
    chat.server("notice", { level: "success", message: "Applied the change to calc.py." });
    assert.match(card.querySelector(".role")?.textContent ?? "", /applied/);
    assert.ok(card.classList.contains("applied"));
  });

  void test("asking about a file shows a banner and enables the composer without a hunk", () => {
    assert.equal((chat.$("send") as HTMLButtonElement).disabled, true);
    chat.send({ kind: "target", file_path: "NOTES.md" });
    assert.equal(chat.$("target").hidden, false);
    assert.match(chat.$("target-label").textContent ?? "", /NOTES\.md/);
    assert.equal((chat.$("send") as HTMLButtonElement).disabled, false);
    chat.click("target-back");
    assert.deepEqual(lastPost(chat), { kind: "backToReview" });
  });

  void test("a file's turns go under an 'About' divider that opens the file", () => {
    chat.server("human_turn", { text: "What is this?", index: -1, total: 2, file_path: "NOTES.md" });
    const divider = chat.doc.querySelector<HTMLButtonElement>(".hunk-divider");
    assert.equal(divider?.textContent, "About NOTES.md");
    divider?.click();
    assert.deepEqual(lastPost(chat), { kind: "openFile", file_path: "NOTES.md" });
  });

  void test("a file read aloud gets its own line, reports each passage, and pauses", () => {
    chat.server("notice", { level: "info", message: "Reading NOTES.md..." });
    const line = chat.doc.querySelector(".turn.reading");
    assert.match(line?.textContent ?? "", /Reading NOTES\.md/);
    const speak = line?.querySelector<HTMLButtonElement>("button.speak");
    assert.equal(speak?.dataset.mode, "loading");
    chat.server("file_audio_chunk", {
      audio_base64: AUDIO,
      mime_type: "audio/wav",
      chunk_index: 0,
      chunk_count: 2,
      file_path: "NOTES.md",
      start_line: 1,
      end_line: 3,
      content_hash: "x",
    });
    assert.deepEqual(lastPost(chat), { kind: "reading", file_path: "NOTES.md", start_line: 1, end_line: 3 });
    assert.equal(speak?.dataset.mode, "playing");
    speak?.click();
    assert.equal(speak?.dataset.mode, "paused");
    chat.server("notice", { level: "success", message: "Finished reading NOTES.md." });
    assert.equal(
      chat.doc.querySelectorAll(".turn.system:not(.reading)").length,
      0,
      "the read's notices stay out of the chat",
    );
  });

  void test("finishing a read aloud doesn't mark a proposal applied", () => {
    chat.send({
      kind: "proposal",
      agent: "Cline",
      summary: "x",
      files: [{ file_path: "calc.py", status: "modified" }],
    });
    chat.server("notice", { level: "success", message: "Finished reading NOTES.md." });
    const card = chat.doc.querySelector(".turn.proposal");
    assert.equal(card?.classList.contains("applied"), false);
  });

  void test("shows the selection that goes with the next question, and can drop it", () => {
    chat.send({ kind: "context", label: "calc.py, lines 1–2" });
    assert.equal(chat.$("context").hidden, false);
    assert.match(chat.$("context-label").textContent ?? "", /calc\.py, lines 1–2/);
    chat.click("context-clear");
    assert.deepEqual(lastPost(chat), { kind: "clearContext" });
    chat.send({ kind: "context", label: null });
    assert.equal(chat.$("context").hidden, true);
  });

  void test("an error replaces the thinking bubble", () => {
    chat.server("presenting", hunk);
    chat.click("explain");
    assert.ok(chat.doc.querySelector(".turn.thinking"));
    chat.server("error", { message: "Model unavailable" });
    assert.equal(chat.doc.querySelector(".turn.thinking"), null);
    assert.equal(chat.doc.querySelector(".turn.error .turn-body")?.textContent, "Model unavailable");
  });
});
