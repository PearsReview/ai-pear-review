import * as vscode from "vscode";

// Speech plays in two places: the chat panel (narration and replies, in its webview)
// and the extension's own player (a markdown file read aloud, audio/player.ts). Each
// claims the voice when it starts, and the other stops, so the two never talk at once.
export type Speaker = "chat" | "file";

export class Speaking implements vscode.Disposable {
  private readonly claims = new vscode.EventEmitter<Speaker>();
  readonly onDidClaim = this.claims.event;

  claim(who: Speaker): void {
    this.claims.fire(who);
  }

  dispose(): void {
    this.claims.dispose();
  }
}
