import * as vscode from "vscode";

import type { Backend } from "../backend/backend.ts";
import { publish } from "../testProbe.ts";

// What the backend reports about actions — notices, errors, an agent stopping, an Act Now
// proposal, the review's end — shown the VS Code way: a status bar message for the
// passing ones, a notification (with its buttons) for the ones that ask something. The
// chat holds the conversation only.

export function register(backend: Backend): vscode.Disposable[] {
  // What was shown, for the integration tests.
  const shown: { kind: string; message: string }[] = [];
  publish("notices.shown", () => shown);

  const status = (message: string): void => {
    shown.push({ kind: "status", message });
    vscode.window.setStatusBarMessage(`$(info) Pear Review: ${message}`, 6000);
  };

  const inform = <T extends string>(message: string, ...buttons: T[]): Thenable<T | undefined> => {
    shown.push({ kind: "info", message });
    return vscode.window.showInformationMessage(`Pear Review: ${message}`, ...buttons);
  };

  // Ended reviews re-send their summary on every reconnect; say it once per ending.
  let summarised = false;

  return [
    backend.on("notice", ({ message, level, event }) => {
      // Read aloud brackets a read with these; its own controls show it.
      if (event === "reading_started" || event === "reading_finished") return;
      if (level === "success") void inform(message);
      else status(message);
    }),
    backend.on("error", ({ message }) => {
      shown.push({ kind: "error", message });
      void vscode.window.showWarningMessage(`Pear Review: ${message}`);
    }),
    backend.on("agent_stopped", ({ message }) => status(message)),
    backend.on("act_now_cleared", ({ message }) => void inform(message)),
    backend.on("act_now_preview", (p) => {
      const files = p.files.length === 1 ? p.files[0]?.file_path : `${p.files.length} files`;
      void inform(
        `${p.agent} proposes a change to ${files}${p.summary ? `: ${p.summary}` : "."}`,
        "Apply",
        "Refine",
        "Discard",
      ).then((choice) => {
        if (choice) void vscode.commands.executeCommand(`pearReview.actNow.${choice.toLowerCase()}`);
      });
    }),
    backend.on("presenting", (p) => {
      if (!p.done || !p.ended) {
        if (!p.done) summarised = false;
        return;
      }
      if (summarised) return;
      summarised = true;
      const pending = p.pending_comment_count ?? 0;
      const parts = [`${p.reviewed_count ?? 0} of ${p.total} changes reviewed`];
      if (pending) parts.push(`${pending} comment${pending === 1 ? "" : "s"} waiting for a plan`);
      const buttons = [
        ...(pending ? ["Create plan"] : []),
        ...(p.review_plan ? ["Open plan"] : []),
        "Start new review",
      ];
      void inform(`${p.ended_early ? "Review ended" : "Review finished"}: ${parts.join(", ")}.`, ...buttons).then(
        (choice) => {
          if (choice === "Create plan") void vscode.commands.executeCommand("pearReview.createPlan");
          else if (choice === "Open plan") void vscode.commands.executeCommand("pearReview.openPlan", p.review_plan);
          else if (choice === "Start new review") void vscode.commands.executeCommand("pearReview.newReview");
        },
      );
    }),
    backend.onStateChange((state) => {
      if (state === "starting") summarised = false;
    }),
  ];
}
