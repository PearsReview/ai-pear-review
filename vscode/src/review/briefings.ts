// What the extension says about briefings (prep_status). Pure, so it's unit-tested
// (test/unit/briefings.test.ts).

import type { BriefingStatus } from "../backend/protocol.ts";

export type HunkBriefing = "current" | "out_of_date";

export function hunkBriefing(status: BriefingStatus | undefined, index: number): HunkBriefing {
  return status?.out_of_date.includes(index) ? "out_of_date" : "current";
}

// The tree row's word for it; nothing when the briefing is up to date.
export function hunkBriefingLabel(state: HunkBriefing): string | undefined {
  return state === "out_of_date" ? "not briefed" : undefined;
}

// Identifies what a warning said, so reconnecting to the same state doesn't repeat it.
export function warningKey(status: BriefingStatus): string {
  return `${status.out_of_date.join(",")}|${status.stale_context.join(",")}`;
}

export const SKILL_INSTRUCTION = "use the prep-review skill";

// An assistant the warning can hand the briefing to: copy what to type, then open its
// chat. The focus commands aren't a published API (checked against Claude Code 2.1 and
// Cline 4.1), so a failure falls back to the copy alone.
export interface BriefingAssistant {
  label: string;
  extensionId: string;
  focusCommand: string;
  // Claude Code runs a skill by name; Cline is asked in words.
  text: string;
}

export const ASSISTANTS: readonly BriefingAssistant[] = [
  {
    label: "Claude Code",
    extensionId: "anthropic.claude-code",
    focusCommand: "claude-vscode.focus",
    text: "/prep-review",
  },
  {
    label: "Cline",
    extensionId: "saoudrizwan.claude-dev",
    focusCommand: "cline.focusChatInput",
    text: SKILL_INSTRUCTION,
  },
];

// The warning's buttons: one per installed assistant, or Copy Instruction without one.
export function briefingButtons(isInstalled: (extensionId: string) => boolean): string[] {
  const installed = ASSISTANTS.filter((a) => isInstalled(a.extensionId)).map((a) => `Brief in ${a.label}`);
  return installed.length ? installed : ["Copy Instruction"];
}
