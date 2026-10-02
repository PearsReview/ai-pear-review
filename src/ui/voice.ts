import * as vscode from "vscode";

import type { Backend } from "../backend/backend.ts";
import { showError } from "../log.ts";

export interface Voice {
  readonly recording: boolean;
  toggle(): void;
  onDidChange: vscode.Event<boolean>;
}

// Press-to-start / press-to-stop: VS Code has no key-up event, so a held key can't
// be push-to-talk. The server records (webviews can't open the microphone) and hands
// the clip back; it goes out as an ordinary voiced `reply`, as the browser's does.
export function register(backend: Backend): { voice: Voice; disposables: vscode.Disposable[] } {
  let recording = false;
  const changes = new vscode.EventEmitter<boolean>();
  const indicator = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 49);
  indicator.text = "$(record) Recording — Ctrl+Alt+Space to send";
  indicator.backgroundColor = new vscode.ThemeColor("statusBarItem.errorBackground");
  indicator.command = "pearReview.toggleRecording";

  const setRecording = (value: boolean): void => {
    recording = value;
    void vscode.commands.executeCommand("setContext", "pearReview.recording", value);
    if (value) indicator.show();
    else indicator.hide();
    changes.fire(value);
  };

  const voice: Voice = {
    get recording() {
      return recording;
    },
    toggle() {
      try {
        backend.send(recording ? "stop_recording" : "start_recording", {});
      } catch (err) {
        showError(err instanceof Error ? err.message : String(err));
      }
    },
    onDidChange: changes.event,
  };

  return {
    voice,
    disposables: [
      indicator,
      changes,
      backend.on("recording_state", ({ recording: value }) => setRecording(value)),
      backend.on("recording_result", ({ audio_base64 }) => {
        try {
          backend.send("reply", { audio_base64 });
        } catch (err) {
          showError(err instanceof Error ? err.message : String(err));
        }
      }),
      backend.onStateChange((state) => {
        if (state !== "ready" && recording) setRecording(false);
      }),
    ],
  };
}
