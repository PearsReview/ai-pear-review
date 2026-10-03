import * as vscode from "vscode";

import type { Backend } from "../backend/backend.ts";

// What the chat is talking about: the hunk on screen (undefined), or a file the
// reviewer asked about (Ask Pear About This File), whose questions go as explore_reply.
// Moving through the review brings the chat back to the hunk, as the browser's Back does.
export interface ChatTarget {
  readonly file: string | undefined;
  setFile(filePath: string | undefined): void;
  readonly onDidChange: vscode.Event<string | undefined>;
}

export function register(backend: Backend): { target: ChatTarget; disposables: vscode.Disposable[] } {
  const changes = new vscode.EventEmitter<string | undefined>();
  let file: string | undefined;
  let shownIndex: number | undefined;

  const target: ChatTarget = {
    get file() {
      return file;
    },
    setFile(filePath) {
      if (filePath === file) return;
      file = filePath;
      changes.fire(file);
    },
    onDidChange: changes.event,
  };

  return {
    target,
    disposables: [
      changes,
      // Only a move to another hunk: the first hunk shown on connect (which may land
      // just after Ask Pear started the backend) and a refresh keep the file.
      backend.on("presenting", (p) => {
        const index = p.done ? undefined : p.index;
        if (shownIndex !== undefined && index !== shownIndex) target.setFile(undefined);
        shownIndex = index;
      }),
      backend.onStateChange((state) => {
        if (state === "ready") return;
        shownIndex = undefined;
        target.setFile(undefined);
      }),
    ],
  };
}
