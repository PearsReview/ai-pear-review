import * as path from "node:path";
import * as vscode from "vscode";

import { log } from "../log.ts";
import { publish } from "../testProbe.ts";
import { nextMessage, NEXT_TIMEOUT_MS, PythonBackend, type Backend, type BackendState } from "./backend.ts";
import type { ClientMessageType, ClientPayloads, ServerMessage, ServerMessageType, ServerPayloads } from "./protocol.ts";

// One backend per repository, behind the one Backend the UI talks to. The UI follows
// the *active* repository: only its backend's messages and states reach the UI, and
// sends go to it. Switching keeps the other processes running, disconnected; switching
// back connects again, and the fresh session resends that review's whole state, while
// the UI resets on the "starting" it passes through. Review state already lives per
// repository (.review/), so nothing is shared between them.
export class BackendManager implements Backend, vscode.Disposable {
  private readonly backends = new Map<string, PythonBackend>();
  private readonly subscriptions = new Map<PythonBackend, vscode.Disposable[]>();
  private active: PythonBackend | undefined;
  private forwardedState: BackendState = "stopped";
  private connectQuery: () => string = () => "";
  // Runs before a repository's backend process starts: what decides how it starts
  // (a pull request review's base commit, see ui/pullRequests.ts) is settled first.
  private beforeStart: (repoPath: string) => Promise<void> = () => Promise.resolve();
  private readonly messages = new vscode.EventEmitter<ServerMessage>();
  private readonly states = new vscode.EventEmitter<BackendState>();
  private readonly sends = new vscode.EventEmitter<ClientMessageType>();
  private readonly repoChanges = new vscode.EventEmitter<string | undefined>();
  readonly onDidSend = this.sends.event;
  // The repository the UI shows, when it changes.
  readonly onDidChangeActiveRepo = this.repoChanges.event;

  constructor(
    private readonly extensionPath: string,
    private readonly secrets: vscode.SecretStorage,
  ) {
    publish("backends", () => ({ shown: this.repoPath, state: this.state, running: this.repos }));
  }

  get state(): BackendState {
    return this.forwardedState;
  }

  get repoPath(): string | undefined {
    return this.active?.repoPath;
  }

  // The repositories with a backend process, running or starting.
  get repos(): string[] {
    return [...this.backends.values()].flatMap((b) =>
      b.repoPath && (b.running || b.state === "starting") ? [b.repoPath] : [],
    );
  }

  async start(repoPath: string): Promise<void> {
    const backend = this.backendFor(repoPath);
    const previous = this.active;
    if (backend === previous) {
      if (backend.state === "ready") return;
      if (backend.running) return backend.resume();
      await this.beforeStart(repoPath);
      return backend.start(repoPath);
    }
    this.active = backend;
    this.repoChanges.fire(repoPath);
    // Its messages stop reaching the UI before the connection drops.
    previous?.disconnect();
    log(`Pear Review: showing ${repoPath}.`);
    if (backend.state === "ready") backend.disconnect();
    this.forward(backend.state === "starting" ? "starting" : "stopped");
    if (backend.running) await backend.resume();
    else {
      await this.beforeStart(repoPath);
      await backend.start(repoPath);
    }
  }

  setBeforeStart(hook: (repoPath: string) => Promise<void>): void {
    this.beforeStart = hook;
  }

  // Stops the active repository's backend.
  async stop(): Promise<void> {
    await this.active?.stop();
  }

  // Stops one repository's backend (it was closed), whichever is active.
  async stopRepo(repoPath: string): Promise<void> {
    const key = keyOf(repoPath);
    const backend = this.backends.get(key);
    if (!backend) return;
    this.backends.delete(key);
    await backend.stop();
    for (const d of this.subscriptions.get(backend) ?? []) d.dispose();
    this.subscriptions.delete(backend);
    backend.dispose();
    if (backend === this.active) {
      this.active = undefined;
      this.forward("stopped");
      this.repoChanges.fire(undefined);
    }
  }

  async stopAll(): Promise<void> {
    await Promise.all([...this.backends.values()].map((b) => b.stop()));
  }

  send<T extends ClientMessageType>(type: T, payload: ClientPayloads[T]): void {
    if (!this.active) throw new Error("The review backend isn't running — start a review first.");
    this.active.send(type, payload);
  }

  on<T extends ServerMessageType>(type: T, listener: (payload: ServerPayloads[T]) => void): vscode.Disposable {
    return this.messages.event((message) => {
      // The union narrows on `type`, but not through a generic parameter.
      if (message.type === type) listener(message.payload as ServerPayloads[T]);
    });
  }

  onStateChange(listener: (state: BackendState) => void): vscode.Disposable {
    return this.states.event(listener);
  }

  next<T extends ServerMessageType>(type: T, timeoutMs = NEXT_TIMEOUT_MS): Promise<ServerPayloads[T]> {
    return nextMessage((t, l) => this.on(t, l), type, timeoutMs);
  }

  reconnect(): Promise<void> {
    if (!this.active) return Promise.reject(new Error("The review backend isn't running."));
    return this.active.reconnect();
  }

  setConnectQuery(query: () => string): void {
    this.connectQuery = query;
    for (const backend of this.backends.values()) backend.setConnectQuery(query);
  }

  dispose(): void {
    void this.stopAll();
    for (const list of this.subscriptions.values()) for (const d of list) d.dispose();
    for (const backend of this.backends.values()) backend.dispose();
    this.messages.dispose();
    this.states.dispose();
    this.sends.dispose();
    this.repoChanges.dispose();
  }

  private backendFor(repoPath: string): PythonBackend {
    const key = keyOf(repoPath);
    const existing = this.backends.get(key);
    if (existing) return existing;
    const backend = new PythonBackend(this.extensionPath, this.secrets);
    backend.setConnectQuery(this.connectQuery);
    this.subscriptions.set(backend, [
      backend.onStateChange((state) => {
        if (backend === this.active) this.forward(state);
      }),
      backend.onDidSend((type) => {
        if (backend === this.active) this.sends.fire(type);
      }),
      backend.onMessage((message) => {
        if (backend === this.active) this.messages.fire(message);
      }),
    ]);
    this.backends.set(key, backend);
    return backend;
  }

  private forward(state: BackendState): void {
    if (state === this.forwardedState) return;
    this.forwardedState = state;
    this.states.fire(state);
  }
}

// Repository roots compared as the file system does: case-insensitively on Windows.
function keyOf(repoPath: string): string {
  const normal = path.resolve(repoPath);
  return process.platform === "win32" ? normal.toLowerCase() : normal;
}
