import { spawn, type ChildProcess } from "node:child_process";
import * as path from "node:path";
import * as vscode from "vscode";

import { resolvePython } from "../backend/python.ts";
import { errorMessage, log } from "../log.ts";

// Speech played by the extension rather than a webview (python/player.py): a webview
// won't play sound until it has been clicked, and Read Aloud is started from the editor
// or the Explorer. The process starts on first use and plays clips in order.

export type PlayerEvent =
  | { event: "start"; id: number }
  | { event: "progress"; id: number; fraction: number }
  | { event: "done"; id: number }
  | { event: "idle" }
  | { event: "error"; message: string };

export class AudioPlayer implements vscode.Disposable {
  private proc: ChildProcess | undefined;
  private nextId = 1;
  private readonly events = new vscode.EventEmitter<PlayerEvent>();
  readonly onEvent = this.events.event;

  constructor(private readonly extensionPath: string) {}

  // Queues a 16-bit WAV clip; returns its id for the events that follow.
  play(wavBase64: string): number {
    const id = this.nextId++;
    this.send({ cmd: "play", id, wav: wavBase64 });
    return id;
  }

  pause(): void {
    this.send({ cmd: "pause" }, false);
  }

  resume(): void {
    this.send({ cmd: "resume" }, false);
  }

  stop(): void {
    this.send({ cmd: "stop" }, false);
  }

  dispose(): void {
    this.send({ cmd: "quit" }, false);
    this.proc?.kill();
    this.proc = undefined;
    this.events.dispose();
  }

  // `start`: whether to launch the player for this command (only playing needs it).
  private send(command: Record<string, unknown>, start = true): void {
    if (!this.proc && start) this.launch();
    const stdin = this.proc?.stdin;
    if (stdin?.writable) stdin.write(JSON.stringify(command) + "\n");
  }

  private launch(): void {
    const script = path.join(this.extensionPath, "python", "player.py");
    let python: string;
    try {
      python = resolvePython(this.extensionPath);
    } catch (err) {
      const message = errorMessage(err);
      log(`Player: ${message}`);
      this.events.fire({ event: "error", message });
      return;
    }
    const proc = spawn(python, ["-u", script], { windowsHide: true });
    this.proc = proc;
    let buffered = "";
    proc.stdout?.on("data", (chunk: Buffer) => {
      buffered += chunk.toString("utf8");
      let newline: number;
      while ((newline = buffered.indexOf("\n")) >= 0) {
        const line = buffered.slice(0, newline).trim();
        buffered = buffered.slice(newline + 1);
        if (!line) continue;
        try {
          this.events.fire(JSON.parse(line) as PlayerEvent);
        } catch {
          log(`Player: unreadable line ${line.slice(0, 120)}`);
        }
      }
    });
    proc.stderr?.on("data", (chunk: Buffer) => log(`Player: ${chunk.toString("utf8").trimEnd()}`));
    // Without these, an interpreter that can't be found, or a write after the player
    // died, is an unhandled 'error' event in the extension host.
    proc.on("error", (err) => this.fail(proc, `Couldn't start the audio player (${err.message}).`));
    proc.stdin?.on("error", (err) => this.fail(proc, `The audio player stopped (${err.message}).`));
    proc.on("exit", (code) => {
      if (code) this.fail(proc, `The audio player stopped (code ${code}).`);
      else if (this.proc === proc) this.proc = undefined;
    });
  }

  // The player is gone (it never started, or its pipe broke): forget it, so the next clip
  // launches a fresh one, and report it once.
  private fail(proc: ChildProcess, message: string): void {
    if (this.proc !== proc) return;
    this.proc = undefined;
    log(`Player: ${message}`);
    this.events.fire({ event: "error", message: `${message} See the log.` });
  }
}
