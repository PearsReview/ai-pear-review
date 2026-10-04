import * as path from "node:path";
import * as vscode from "vscode";

import type { BackendManager } from "../backend/manager.ts";
import { gitApi, repositoryRoots } from "../git.ts";
import { showError } from "../log.ts";
import { publish } from "../testProbe.ts";

export interface Repos {
  // More than one repository is open, so the UI names the one it shows.
  readonly multi: boolean;
  readonly onDidChange: vscode.Event<void>;
}

// The workspace's git repositories, as the Changes view needs them: whether there are
// none (its welcome says to open one), several (a picker, and the view names the one
// shown), and a repository closing (its backend stops).
export function register(manager: BackendManager): { repos: Repos; disposables: vscode.Disposable[] } {
  const changes = new vscode.EventEmitter<void>();
  let roots: string[] = [];
  publish("repos.roots", () => roots);

  const refresh = (list: string[]): void => {
    roots = list;
    void vscode.commands.executeCommand("setContext", "pearReview.noRepo", roots.length === 0);
    void vscode.commands.executeCommand("setContext", "pearReview.multiRepo", roots.length > 1);
    changes.fire();
  };

  const current = async (): Promise<string[]> => (await gitApi())?.repositories.map((r) => r.rootUri.fsPath) ?? [];

  // `preset` (a repository root) skips the picker, for a keybinding or the tests.
  const switchRepository = async (preset?: unknown): Promise<void> => {
    const list = await repositoryRoots();
    if (!list.length) {
      showError("Open a git repository to start a review.");
      return;
    }
    const given = list.find((root) => typeof preset === "string" && path.relative(root, preset) === "");
    const picked = given
      ? { label: path.basename(given), root: given }
      : await vscode.window.showQuickPick(
          list.map((root) => ({
            label: path.basename(root),
            description: root === manager.repoPath ? "shown" : manager.repos.includes(root) ? "running" : undefined,
            detail: root,
            root,
          })),
          { title: "Repository to review" },
        );
    if (!picked || picked.root === manager.repoPath) return;
    try {
      await vscode.window.withProgress(
        { location: vscode.ProgressLocation.Window, title: `Pear Review: opening ${picked.label}` },
        () => manager.start(picked.root),
      );
    } catch (err) {
      showError(err instanceof Error ? err.message : String(err));
    }
  };

  const disposables: vscode.Disposable[] = [
    changes,
    vscode.commands.registerCommand("pearReview.switchRepository", switchRepository),
  ];
  void repositoryRoots().then(refresh);
  void gitApi().then((git) => {
    if (!git) return;
    disposables.push(
      git.onDidOpenRepository(() => void current().then(refresh)),
      git.onDidCloseRepository(({ rootUri }) => {
        void manager.stopRepo(rootUri.fsPath);
        void current().then(refresh);
      }),
    );
  });

  return {
    repos: {
      get multi() {
        return roots.length > 1;
      },
      onDidChange: changes.event,
    },
    disposables: [
      {
        dispose: () => {
          for (const d of disposables) d.dispose();
        },
      },
    ],
  };
}
