import * as path from "node:path";
import * as vscode from "vscode";

import type { Backend } from "../backend/backend.ts";
import type { ActNowPreview, MarkedLine, ProposedFile } from "../backend/protocol.ts";
import { showError } from "../log.ts";
import { sidesOf } from "../review/hunks.ts";

// Read-only documents for a proposal's two sides: pear-proposal:/before/<path> and
// pear-proposal:/after/<path>. Nothing is on disk until Apply.
const SCHEME = "pear-proposal";

export interface ActNow {
  // Act mode: the next typed or spoken message goes to the agent, not the reviewer.
  readonly active: boolean;
  setActive(on: boolean): void;
  readonly onDidChangeActive: vscode.Event<boolean>;
  // Sends one instruction (typed or recorded) and leaves act mode.
  request(instruction: { text: string } | { audio_base64: string }, marked_lines?: MarkedLine[]): void;
  readonly proposal: ActNowPreview | undefined;
}

export function register(backend: Backend): { actNow: ActNow; disposables: vscode.Disposable[] } {
  const contents = new Map<string, string>();
  const docChanges = new vscode.EventEmitter<vscode.Uri>();
  const activeChanges = new vscode.EventEmitter<boolean>();
  let active = false;
  let proposal: ActNowPreview | undefined;

  const uriFor = (side: "before" | "after", filePath: string): vscode.Uri =>
    vscode.Uri.from({ scheme: SCHEME, path: `/${side}/${filePath}` });

  const setActive = (on: boolean): void => {
    if (on === active) return;
    active = on;
    void vscode.commands.executeCommand("setContext", "pearReview.actMode", on);
    activeChanges.fire(on);
  };

  const openFile = async (file: ProposedFile): Promise<void> => {
    const name = path.posix.basename(file.file_path);
    const agent = proposal?.agent ?? "agent";
    await vscode.commands.executeCommand(
      "vscode.diff",
      uriFor("before", file.file_path),
      uriFor("after", file.file_path),
      `${name} (proposed by ${agent}, ${file.status})`,
      { preview: false },
    );
  };

  const closeProposalTabs = async (): Promise<void> => {
    const tabs = vscode.window.tabGroups.all
      .flatMap((group) => group.tabs)
      .filter((tab) => tab.input instanceof vscode.TabInputTextDiff && tab.input.modified.scheme === SCHEME);
    if (tabs.length) await vscode.window.tabGroups.close(tabs);
  };

  const clear = (): void => {
    proposal = undefined;
    void vscode.commands.executeCommand("setContext", "pearReview.hasProposal", false);
    void closeProposalTabs();
  };

  const guarded = (fn: () => void): void => {
    try {
      fn();
    } catch (err) {
      showError(err instanceof Error ? err.message : String(err));
    }
  };

  const actNow: ActNow = {
    get active() {
      return active;
    },
    setActive,
    onDidChangeActive: activeChanges.event,
    request(instruction, marked_lines) {
      backend.send("act_now", marked_lines ? { ...instruction, marked_lines } : instruction);
      setActive(false);
    },
    get proposal() {
      return proposal;
    },
  };

  return {
    actNow,
    disposables: [
      docChanges,
      activeChanges,
      vscode.workspace.registerTextDocumentContentProvider(SCHEME, {
        onDidChange: docChanges.event,
        provideTextDocumentContent: (uri) => contents.get(uri.toString()) ?? "",
      }),
      backend.on("act_now_preview", (preview) => {
        proposal = preview;
        for (const file of preview.files) {
          const { before, after } = sidesOf(file.full_lines);
          for (const [side, text] of [
            ["before", before],
            ["after", after],
          ] as const) {
            const uri = uriFor(side, file.file_path);
            contents.set(uri.toString(), text);
            // A refined proposal reuses the same documents; this redraws open tabs.
            docChanges.fire(uri);
          }
        }
        void vscode.commands.executeCommand("setContext", "pearReview.hasProposal", true);
        const first = preview.files[0];
        if (first) void openFile(first);
      }),
      backend.on("act_now_cleared", clear),
      backend.on("notice", (n) => {
        // confirm_act_now's only success signal; the review diff refreshes on its own.
        if (n.level === "success" && n.message.startsWith("Applied the change")) clear();
      }),
      backend.onStateChange((state) => {
        if (state !== "ready") {
          clear();
          setActive(false);
        }
      }),
      vscode.commands.registerCommand("pearReview.actNow.toggle", () => setActive(!active)),
      vscode.commands.registerCommand("pearReview.actNow.apply", () => {
        if (proposal) guarded(() => backend.send("confirm_act_now", {}));
      }),
      vscode.commands.registerCommand("pearReview.actNow.refine", async (text?: unknown) => {
        if (!proposal) return;
        const instruction =
          typeof text === "string" && text.trim()
            ? text.trim()
            : await vscode.window.showInputBox({
                title: `Refine ${proposal.agent}'s proposal`,
                prompt: "What should change about it?",
                ignoreFocusOut: true,
              });
        if (instruction?.trim()) guarded(() => backend.send("refine_act_now", { text: instruction.trim() }));
      }),
      vscode.commands.registerCommand("pearReview.actNow.discard", clear),
      vscode.commands.registerCommand("pearReview.actNow.openFile", (filePath: unknown) => {
        const file = proposal?.files.find((f) => f.file_path === filePath);
        if (file) void openFile(file);
      }),
    ],
  };
}
