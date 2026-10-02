import * as vscode from "vscode";

import { log } from "../log.ts";
import { BackendProcess } from "./backendProcess.ts";
import type { ClientMessageType, ClientPayloads, ServerMessage, ServerMessageType, ServerPayloads } from "./protocol.ts";
import { resolvePython } from "./python.ts";
import { WsClient } from "./wsClient.ts";

export type BackendState = "stopped" | "starting" | "ready" | "error";

// The one seam every UI module talks through. Python sits behind it today; any piece
// can be reimplemented in TypeScript later without the UI noticing.
export interface Backend {
  readonly state: BackendState;
  readonly repoPath: string | undefined;
  start(repoPath: string): Promise<void>;
  stop(): Promise<void>;
  send<T extends ClientMessageType>(type: T, payload: ClientPayloads[T]): void;
  on<T extends ServerMessageType>(type: T, listener: (payload: ServerPayloads[T]) => void): vscode.Disposable;
  onStateChange(listener: (state: BackendState) => void): vscode.Disposable;
}

// A dropped socket with the process still alive is retried once; anything worse is
// an error state the user restarts from.
const RECONNECT_DELAY_MS = 1_000;

export class PythonBackend implements Backend, vscode.Disposable {
  private _state: BackendState = "stopped";
  private _repoPath: string | undefined;
  private process: BackendProcess | undefined;
  private client: WsClient | undefined;
  private port: number | undefined;
  private reconnecting = false;
  private readonly messages = new vscode.EventEmitter<ServerMessage>();
  private readonly states = new vscode.EventEmitter<BackendState>();

  constructor(
    private readonly extensionPath: string,
    private readonly secrets: vscode.SecretStorage,
  ) {}

  get state(): BackendState {
    return this._state;
  }

  get repoPath(): string | undefined {
    return this._repoPath;
  }

  async start(repoPath: string): Promise<void> {
    if (this._repoPath === repoPath && (this._state === "ready" || this._state === "starting")) return;
    await this.stop();
    this._repoPath = repoPath;
    this.setState("starting");
    const proc = new BackendProcess();
    this.process = proc;
    proc.onExit((code) => {
      if (this.process !== proc) return;
      log(`Backend exited (code ${code}).`);
      this.process = undefined;
      this.client?.close();
      this.client = undefined;
      this.setState("error");
    });
    try {
      const apiKey = await this.secrets.get("pearReview.anthropicApiKey");
      this.port = await proc.start({
        python: resolvePython(this.extensionPath),
        extensionPath: this.extensionPath,
        repoPath,
        env: apiKey ? { ANTHROPIC_API_KEY: apiKey } : {},
      });
      await this.connect();
      this.setState("ready");
    } catch (err) {
      await this.stop();
      this.setState("error");
      throw err;
    }
  }

  async stop(): Promise<void> {
    const proc = this.process;
    this.process = undefined;
    this.client?.close();
    this.client = undefined;
    this.port = undefined;
    await proc?.stop();
    if (this._state !== "error") this.setState("stopped");
  }

  send<T extends ClientMessageType>(type: T, payload: ClientPayloads[T]): void {
    if (!this.client) throw new Error("The review backend isn't running — start a review first.");
    this.client.send(type, payload);
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

  dispose(): void {
    void this.stop();
    this.messages.dispose();
    this.states.dispose();
  }

  private async connect(): Promise<void> {
    const client = new WsClient(
      (message) => this.messages.fire(message),
      () => this.onSocketClosed(client),
    );
    this.client = client;
    await client.connect(this.port as number);
  }

  private onSocketClosed(client: WsClient): void {
    if (this.client !== client || !this.process || this.reconnecting) return;
    this.reconnecting = true;
    log("Lost the backend connection; reconnecting once.");
    setTimeout(() => {
      this.connect()
        .catch((err: unknown) => {
          log(`Reconnect failed: ${String(err)}`);
          this.setState("error");
        })
        .finally(() => (this.reconnecting = false));
    }, RECONNECT_DELAY_MS);
  }

  private setState(state: BackendState): void {
    if (this._state === state) return;
    this._state = state;
    this.states.fire(state);
  }
}
