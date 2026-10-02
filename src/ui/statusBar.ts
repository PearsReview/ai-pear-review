import * as vscode from "vscode";

import type { Backend, BackendState } from "../backend/backend.ts";
import type { ServiceStatus } from "../backend/protocol.ts";

const STATE_TEXT: Record<BackendState, string> = {
  stopped: "$(circle-outline) Pear",
  starting: "$(loading~spin) Pear",
  ready: "$(pass) Pear",
  error: "$(error) Pear",
};

// Backend state, plus which services last reported in (service_status is sent
// piecemeal — each message carries only the services it learned about).
export function register(backend: Backend): vscode.Disposable[] {
  const item = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 50);
  item.command = "pearReview.showLog";
  let services: ServiceStatus = {};

  const render = (): void => {
    item.text = STATE_TEXT[backend.state];
    const lines = [`Pear Review backend: ${backend.state}`];
    if (backend.state === "ready") {
      for (const [name, up] of Object.entries(services)) {
        if (typeof up === "boolean") lines.push(`${name}: ${up ? "up" : "down"}`);
      }
    }
    item.tooltip = lines.join("\n");
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
      services = { ...services, ...status };
      render();
    }),
  ];
}
