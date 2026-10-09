import * as vscode from "vscode";

import type { Backend } from "../backend/backend.ts";
import type { BriefingStatus } from "../backend/protocol.ts";
import {
  ASSISTANTS,
  SKILL_INSTRUCTION,
  briefingButtons,
  hunkBriefing,
  warningKey,
  type BriefingAssistant,
  type HunkBriefing,
} from "../review/briefings.ts";
import { publish } from "../testProbe.ts";

// Pear's explanations are only as good as the briefings behind them. When the review
// opens (or the diff is refreshed) and some changes have no up-to-date briefing, this
// says so, once per state, with a button per installed assistant (Claude Code, Cline)
// that copies the prep-review instruction and opens its chat: the session that made
// the change knows why, which no later run can. The Changes view marks each such
// change "not briefed" (hunkTree.ts).

export interface Briefings {
  state(index: number): HunkBriefing;
  readonly onDidChange: vscode.Event<void>;
}

export function register(backend: Backend): { briefings: Briefings; disposables: vscode.Disposable[] } {
  const changed = new vscode.EventEmitter<void>();
  let status: BriefingStatus | undefined;
  // What the last warning said, until the backend stops: a reconnect to the same state
  // (after a settings change) doesn't repeat it, but opening the changes again does.
  let warned: string | undefined;
  const shown: string[] = [];
  let shownButtons: string[] = [];
  publish("briefings.warnings", () => shown);
  publish("briefings.buttons", () => shownButtons);
  publish("briefings.status", () => status);

  const copyInstruction = async (): Promise<void> => {
    await vscode.env.clipboard.writeText(SKILL_INSTRUCTION);
    void vscode.window.showInformationMessage(
      `Copied "${SKILL_INSTRUCTION}". Paste it into Claude Code or Cline, open in this repository.`,
    );
  };

  // Copy what to type, then open the assistant's chat to paste it into.
  const briefIn = async (assistant: BriefingAssistant): Promise<void> => {
    await vscode.env.clipboard.writeText(assistant.text);
    try {
      await vscode.commands.executeCommand(assistant.focusCommand);
      void vscode.window.showInformationMessage(
        `Copied "${assistant.text}". Paste it into ${assistant.label} to brief the changes made in this repository.`,
      );
    } catch {
      void vscode.window.showInformationMessage(
        `Copied "${assistant.text}". Open ${assistant.label} in this repository and paste it there.`,
      );
    }
  };

  const warn = async (s: BriefingStatus): Promise<void> => {
    if (!s.message) return;
    shown.push(s.message);
    const buttons = s.out_of_date.length ? briefingButtons((id) => !!vscode.extensions.getExtension(id)) : [];
    shownButtons = buttons;
    const choice = await vscode.window.showWarningMessage(`Pear Review: ${s.message}`, ...buttons);
    const assistant = ASSISTANTS.find((a) => choice === `Brief in ${a.label}`);
    if (assistant) await briefIn(assistant);
    else if (choice === "Copy Instruction") await copyInstruction();
  };

  const update = (s: BriefingStatus | undefined): void => {
    status = s;
    changed.fire();
  };

  return {
    briefings: { state: (index) => hunkBriefing(status, index), onDidChange: changed.event },
    disposables: [
      changed,
      backend.on("prep_status", (s) => {
        update(s);
        if (s.message && warningKey(s) !== warned) {
          warned = warningKey(s);
          void warn(s);
        }
      }),
      backend.onStateChange((state) => {
        if (state !== "stopped" && state !== "error") return;
        warned = undefined;
        update(undefined);
      }),
    ],
  };
}
