import * as vscode from "vscode";

import type { Backend } from "../backend/backend.ts";

// The reviewer's three preferences from the web app's settings panel. The backend holds
// them per connection (set_voice_prefs, set_narration_prefs), so the extension keeps
// them across restarts and sends them whenever a connection opens.
export interface PrefValues {
  // Explain each change as the review reaches it, or only on request.
  autoNarrate: boolean;
  // Speak narration and replies aloud automatically. The speaker button works either way.
  tts: boolean;
  // Voice input (the mic).
  stt: boolean;
}

export interface Prefs {
  readonly values: PrefValues;
  set(change: Partial<PrefValues>): void;
  readonly onDidChange: vscode.Event<PrefValues>;
}

const KEY = "pearReview.prefs";
const DEFAULTS: PrefValues = { autoNarrate: true, tts: true, stt: true };

export function register(
  context: vscode.ExtensionContext,
  backend: Backend,
): { prefs: Prefs; disposables: vscode.Disposable[] } {
  const changes = new vscode.EventEmitter<PrefValues>();
  let values: PrefValues = { ...DEFAULTS, ...context.globalState.get<Partial<PrefValues>>(KEY) };

  const send = (): void => {
    if (backend.state !== "ready") return;
    backend.send("set_voice_prefs", { stt_enabled: values.stt, tts_enabled: values.tts });
    backend.send("set_narration_prefs", { auto_narrate: values.autoNarrate });
  };

  const prefs: Prefs = {
    get values() {
      return values;
    },
    set(change) {
      values = { ...values, ...change };
      void context.globalState.update(KEY, values);
      send();
      changes.fire(values);
    },
    onDidChange: changes.event,
  };

  // The first hunk is presented before any message could arrive, so automatic
  // explanation also rides on the connection URL (see websocket_endpoint in server.py).
  backend.setConnectQuery(() => (values.autoNarrate ? "" : "auto_narrate=0"));

  return {
    prefs,
    disposables: [
      changes,
      backend.onStateChange((state) => {
        if (state === "ready") send();
      }),
    ],
  };
}
