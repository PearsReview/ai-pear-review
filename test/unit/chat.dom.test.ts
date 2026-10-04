// The chat webview's own behaviour (media/chat/chat.js) in a DOM: what it renders for
// each message from the extension, and what it posts back when the reviewer clicks or
// types. The integration suite covers the extension side of the same messages.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { afterEach, beforeEach, describe, test } from "node:test";

import { JSDOM } from "jsdom";

import { chatHtml } from "../../src/ui/chatHtml.ts";

const media = (file: string): string => readFileSync(new URL(`../../media/chat/${file}`, import.meta.url), "utf8");
const scripts = [media("blocks.js"), media("readalong.js"), media("audio.js"), media("chat.js")].join("\n");

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
  duration = 10;
  // Moves playback to `seconds` and fires timeupdate, as the browser does while playing.
  seek(seconds: number): void {
    this.currentTime = seconds;
    for (const fn of this.listeners.get("timeupdate") ?? []) fn();
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
    assert.match(chat.doc.querySelector(".audio-hint")?.textContent ?? "", /needs one click/);
    assert.ok(
      chat.posts.some((m) => m.kind === "audioBlocked"),
      "the extension is told, for its log",
    );
    speak?.click();
    assert.equal(FakeAudio.last?.paused, false, "the click starts playback");
    assert.equal(speak?.dataset.mode, "playing");
    assert.equal(chat.doc.querySelector(".audio-hint"), null, "the hint goes once it plays");
  });

  void test("the gear opens settings, and voice input off disables the mic", () => {
    chat.server("presenting", hunk);
    chat.click("settings");
    assert.deepEqual(lastPost(chat), { kind: "command", command: "settings" });
    assert.equal((chat.$("mic") as HTMLButtonElement).disabled, false);
    chat.send({ kind: "prefs", prefs: { autoNarrate: true, tts: true, stt: false } });
    assert.equal((chat.$("mic") as HTMLButtonElement).disabled, true);
    assert.match(chat.$("mic").title, /Voice input is off/);
  });

  void test("the read-along follows the voice through a reply's sentences", () => {
    chat.server("presenting", hunk);
    chat.server("reviewer_turn", {
      text: "First sentence here. Second one follows.",
      spoken: "First sentence here. Second one follows.",
      index: 0,
      total: 2,
      file_path: "calc.py",
    });
    chat.doc.querySelector<HTMLButtonElement>(".turn.presenter button.speak")?.click();
    chat.server("turn_audio_chunk", {
      audio_base64: AUDIO,
      mime_type: "audio/wav",
      chunk_index: 0,
      chunk_count: 1,
      sentences: [
        { text: "First sentence here.", weight: 1 },
        { text: "Second one follows.", weight: 1 },
      ],
    });
    const turn = chat.doc.querySelector<HTMLElement>(".turn.presenter");
    FakeAudio.last?.seek(2);
    assert.equal(turn?.dataset.readingSentence, "0");
    FakeAudio.last?.seek(8);
    assert.equal(turn?.dataset.readingSentence, "1");
  });

  void test("the filter shows only the current file's conversation", () => {
    chat.server("presenting", hunk);
    chat.server("narration", narration);
    chat.server("presenting", { ...hunk, index: 1, file_path: "other.py" });
    chat.server("narration", { ...narration, index: 1, file_path: "other.py", text: "Other." });
    chat.click("filter");
    const visible = [...chat.doc.querySelectorAll<HTMLElement>(".turn.presenter")].filter(
      (t) => !t.classList.contains("filtered"),
    );
    assert.deepEqual(
      visible.map((t) => t.dataset.file),
      ["other.py"],
    );
    assert.equal(chat.$("filter").getAttribute("aria-pressed"), "true");
    chat.click("filter");
    assert.equal(chat.doc.querySelectorAll(".turn.filtered").length, 0);
  });

  void test("a change too large for the model offers a hand-off to copy", () => {
    chat.server("presenting", hunk);
    chat.server("context_too_large", {
      kind: "hunk",
      reason: "too_large",
      file_path: "calc.py",
      question: null,
      estimated_tokens: 12000,
      budget_tokens: 7000,
      handoff_text: "In this repo, look at calc.py…",
    });
    const card = chat.doc.querySelector(".turn.too-large");
    assert.match(
      card?.textContent ?? "",
      /doesn't fit in the model's context \(about 12,000 tokens; it can read 7,000\)/,
    );
    [...(card?.querySelectorAll<HTMLButtonElement>("button") ?? [])]
      .find((b) => b.textContent === "Copy for your agent")
      ?.click();
    assert.deepEqual(lastPost(chat), { kind: "copy", text: "In this repo, look at calc.py…" });
  });

  void test("an error or a stopped agent ends the wait, and stays out of the chat", () => {
    chat.server("presenting", hunk);
    chat.click("explain");
    assert.ok(chat.doc.querySelector(".turn.thinking"));
    chat.send({ kind: "settle" });
    assert.equal(chat.doc.querySelector(".turn.thinking"), null);
    chat.server("error", { message: "Model unavailable" });
    chat.server("notice", { level: "success", message: "Applied the change to calc.py." });
    assert.equal(chat.doc.querySelectorAll(".turn").length, 0, "only conversation turns go in the chat");
  });

  void test("the review's end is reported outside the chat", () => {
    chat.server("presenting", {
      index: 0,
      total: 5,
      done: true,
      ended: true,
      reviewed_count: 5,
      pending_comment_count: 0,
    });
    assert.equal(chat.doc.querySelectorAll("#transcript > *").length, 0);
    assert.match(chat.$("hunk").textContent ?? "", /ended/);
  });

  void test("sends a typed question on Enter", () => {
    chat.server("presenting", hunk);
    const input = chat.$("input") as HTMLTextAreaElement;
    input.value = "Why?";
    input.dispatchEvent(new chat.window.KeyboardEvent("keydown", { key: "Enter" }));
    assert.deepEqual(lastPost(chat), { kind: "send", text: "Why?" });
    assert.equal(input.value, "");
  });

  void test("suggests questions for the change, the selection, a file and act mode", () => {
    const chips = (): string[] =>
      [...chat.doc.querySelectorAll("#suggestions .suggestion")].map((c) => c.textContent ?? "");
    assert.equal(chat.$("suggestions").hidden, true, "nothing to ask about yet, no chips");
    chat.server("presenting", hunk);
    assert.ok(chips().includes("Why was this modified?"));
    chat.send({ kind: "context", label: "calc.py, lines 2–4" });
    assert.ok(chips().includes("Explain this code"));
    chat.send({ kind: "context", label: null });
    chat.send({ kind: "target", file_path: "NOTES.md" });
    assert.ok(chips().includes("Explain what this file does"));
    chat.send({ kind: "actMode", on: true });
    assert.ok(chips().includes("Add type hints"));
  });

  void test("a chip fills the message box without sending", () => {
    chat.server("presenting", hunk);
    const before = chat.posts.length;
    const chip = [...chat.doc.querySelectorAll<HTMLButtonElement>("#suggestions .suggestion")].find(
      (c) => c.textContent === "Suggest a test for this",
    );
    chip?.click();
    assert.equal((chat.$("input") as HTMLTextAreaElement).value, "Suggest a test for this");
    assert.equal(chat.posts.length, before);
  });

  void test("before the review starts, a first change says how to begin", () => {
    chat.server("presenting", { ...hunk, review_started: false });
    assert.match(chat.doc.querySelector("#transcript .turn.system")?.textContent ?? "", /start the review/);
  });

  void test("an Enter that commits an IME candidate doesn't send", () => {
    chat.server("presenting", hunk);
    const input = chat.$("input") as HTMLTextAreaElement;
    input.value = "なぜ";
    const before = chat.posts.length;
    input.dispatchEvent(new chat.window.KeyboardEvent("keydown", { key: "Enter", isComposing: true }));
    assert.equal(chat.posts.length, before);
    assert.equal(input.value, "なぜ");
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

  void test("reading a file aloud leaves the chat alone", () => {
    chat.server("notice", { level: "info", message: "Reading NOTES.md..." });
    chat.server("notice", { level: "success", message: "Finished reading NOTES.md." });
    assert.equal(chat.doc.querySelectorAll("#transcript > *").length, 0);
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
});
