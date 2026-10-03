import * as vscode from "vscode";

import type { Backend, BackendState } from "../backend/backend.ts";
import { downNames, statusRows, tokenLine, type Services } from "../review/status.ts";

const STATE_TEXT: Record<BackendState, string> = {
  stopped: "$(circle-outline) Pear",
  starting: "$(loading~spin) Pear",
  ready: "$(pass) Pear",
  error: "$(error) Pear",
};

const MARK = { up: "$(pass)", down: "$(error)", unknown: "$(circle-outline)", off: "$(circle-slash)" } as const;

// The web app's service pills, the VS Code way: the status bar item names any service
// that is down, and its hover lists them all with this session's token use.
export function register(backend: Backend): vscode.Disposable[] {
  const item = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 50);
  item.command = "pearReview.showLog";
  item.name = "Pear Review";
  let services: Services = {};

  const render = (): void => {
    const rows = statusRows(services);
    const down = backend.state === "ready" ? downNames(rows) : [];
    item.text = down.length ? `$(warning) Pear · ${down.join(", ")} down` : STATE_TEXT[backend.state];
    item.backgroundColor = down.length ? new vscode.ThemeColor("statusBarItem.warningBackground") : undefined;

    const tip = new vscode.MarkdownString(undefined, true);
    tip.appendMarkdown(`**Pear Review** — backend ${backend.state}\n\n`);
    if (backend.state === "ready") {
      for (const row of rows) {
        tip.appendMarkdown(`${MARK[row.state]} ${row.name}: ${row.state}${row.note ? ` (${row.note})` : ""}  \n`);
      }
      const tokens = tokenLine(services);
      if (tokens) tip.appendMarkdown(`\n${tokens}\n`);
    }
    tip.appendMarkdown("\nClick for the log.");
    item.tooltip = tip;
    item.show();
  };

  render();
  return [
    item,
    backend.onStateChange((state) => {
      if (state === "starting") services = {};
      render();
    }),
    backend.on("service_status", (status) => {
      services = { ...services, ...(status as Services) };
      render();
    }),
  ];
}
