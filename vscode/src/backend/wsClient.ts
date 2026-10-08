import WebSocket from "ws";

import { log } from "../log.ts";
import { isServerMessage, type ClientMessageType, type ClientPayloads, type ServerMessage } from "./protocol.ts";

// The socket to one backend. Sends typed client messages and hands each validated
// server message to `onMessage`; reconnecting is the caller's decision, not this one's.
export class WsClient {
  private socket: WebSocket | undefined;

  constructor(
    private readonly onMessage: (message: ServerMessage) => void,
    private readonly onClose: () => void,
  ) {}

  // `query` is the connection URL's query string, without the "?".
  connect(port: number, query = ""): Promise<void> {
    const origin = `http://127.0.0.1:${port}`;
    // The server rejects a handshake without an Origin that matches its Host (its
    // CSWSH guard, _origin_is_trusted in app/server.py), and ws sends none by default.
    const socket = new WebSocket(`ws://127.0.0.1:${port}/ws${query ? `?${query}` : ""}`, { origin });
    this.socket = socket;
    socket.on("message", (data: WebSocket.RawData) => this.receive(data));
    socket.on("close", () => {
      if (this.socket === socket) this.socket = undefined;
      this.onClose();
    });
    return new Promise((resolve, reject) => {
      socket.once("open", () => resolve());
      socket.once("error", (err) => reject(err));
    });
  }

  send<T extends ClientMessageType>(type: T, payload: ClientPayloads[T]): void {
    if (!this.socket || this.socket.readyState !== WebSocket.OPEN) {
      throw new Error("Not connected to the review backend.");
    }
    this.socket.send(JSON.stringify({ type, payload }));
  }

  close(): void {
    this.socket?.close();
    this.socket = undefined;
  }

  private receive(data: WebSocket.RawData): void {
    const text = rawToString(data);
    let parsed: unknown;
    try {
      parsed = JSON.parse(text);
    } catch {
      log(`Dropped a non-JSON message from the backend.`);
      return;
    }
    if (isServerMessage(parsed)) {
      this.onMessage(parsed);
    } else {
      log(`Dropped an unknown message from the backend: ${text.slice(0, 200)}`);
    }
  }
}

function rawToString(data: WebSocket.RawData): string {
  if (Array.isArray(data)) return Buffer.concat(data).toString("utf8");
  if (Buffer.isBuffer(data)) return data.toString("utf8");
  return Buffer.from(new Uint8Array(data)).toString("utf8");
}
