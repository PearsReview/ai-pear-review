import * as path from "node:path";
import * as vscode from "vscode";

import type { Backend } from "../backend/backend.ts";
import type { ProgressFile, ProgressHunk, ReviewProgress } from "../backend/protocol.ts";
import { fileChange, hunkLabel } from "../review/hunks.ts";
import { publish } from "../testProbe.ts";
import type { Comments } from "./comments.ts";
import type { Reader } from "./files.ts";
import { showPreview } from "./previewTabs.ts";

type Node = { kind: "file"; file: ProgressFile } | { kind: "hunk"; hunk: ProgressHunk; file: ProgressFile };

// Files → hunks, from review_progress (which the backend resends on every move and
// every reviewed toggle), with the hunk on screen marked and revealed.
export function register(backend: Backend, comments: Comments, reader: Reader): vscode.Disposable[] {
  const provider = new HunkTreeProvider(
    () => backend.repoPath,
    () => reader.state,
  );
  const view = vscode.window.createTreeView("pearReview.hunks", { treeDataProvider: provider });
  // Showing the view opens the changes (pearReview.openChanges, quietly): browsing and
  // the chat don't wait for Start Review.
  // The first time the changes open, the chat's tab in the secondary side bar (beside
  // other chat extensions) comes forward too, and focus returns to the tree.
  let chatShown = false;
  const openWhenShown = (): void => {
    if (!view.visible || backend.state !== "stopped") return;
    void vscode.commands.executeCommand<boolean>("pearReview.openChanges", { quiet: true }).then(async (opened) => {
      if (!opened || chatShown) return;
      chatShown = true;
      await vscode.commands.executeCommand("pearReview.chat.focus");
      await vscode.commands.executeCommand("pearReview.hunks.focus");
    });
  };
  openWhenShown();
  publish("tree.view", () => ({
    description: view.description,
    reviewStarted: progress?.review_started ?? false,
    files: provider.getChildren().map((file) => ({
      ...itemSummary(provider.getTreeItem(file)),
      hunks: provider.getChildren(file).map((hunk) => itemSummary(provider.getTreeItem(hunk))),
    })),
  }));

  // "presenting" comes just before the "review_progress" that rebuilds the nodes, so
  // both reveal: the second one lands on the rebuilt node.
  const revealCurrent = (): void => {
    const node = provider.currentNode();
    if (node && view.visible) void view.reveal(node, { select: true, focus: false });
  };

  let progress: ReviewProgress | undefined;
  const describe = (): void => {
    const parts = progress?.total ? [`${progress.reviewed_count}/${progress.total} reviewed`] : [];
    if (comments.count) parts.push(`${comments.count} comment${comments.count === 1 ? "" : "s"}`);
    view.description = parts.join(" · ") || undefined;
  };

  const fileUri = (node: Node | undefined): vscode.Uri | undefined =>
    node && backend.repoPath ? vscode.Uri.file(path.join(backend.repoPath, node.file.file_path)) : undefined;

  return [
    view,
    provider,
    view.onDidChangeVisibility(openWhenShown),
    // A markdown row's speaker follows its read: pause, play, stop.
    reader.onDidChange(() => provider.refresh()),
    // The row passes its node; these hand its file to VS Code's preview and to Read Aloud.
    vscode.commands.registerCommand("pearReview.tree.preview", (node: Node) => {
      const uri = fileUri(node);
      if (uri) void showPreview(uri);
    }),
    vscode.commands.registerCommand("pearReview.tree.readAloud", (node: Node) => {
      const uri = fileUri(node);
      if (uri) void vscode.commands.executeCommand("pearReview.readAloud", uri);
    }),
    comments.onDidChangeCount(describe),
    backend.on("review_progress", (p) => {
      progress = p;
      void vscode.commands.executeCommand("setContext", "pearReview.reviewStarted", p.review_started);
      void vscode.commands.executeCommand("setContext", "pearReview.reviewEnded", p.review_ended);
      provider.setProgress(p);
      describe();
      revealCurrent();
    }),
    backend.on("presenting", (p) => {
      provider.setCurrent(p.done ? undefined : p.index);
      revealCurrent();
    }),
    backend.onStateChange((state) => {
      if (state === "stopped" || state === "error") {
        progress = undefined;
        provider.setProgress(undefined);
        describe();
      }
    }),
  ];
}

function itemSummary(item: vscode.TreeItem): Record<string, unknown> {
  return {
    label: typeof item.label === "string" ? item.label : item.label?.label,
    description: item.description,
    contextValue: item.contextValue,
    icon: item.iconPath instanceof vscode.ThemeIcon ? item.iconPath.id : undefined,
    command: item.command?.arguments,
  };
}

class HunkTreeProvider implements vscode.TreeDataProvider<Node>, vscode.Disposable {
  private readonly changes = new vscode.EventEmitter<void>();
  readonly onDidChangeTreeData = this.changes.event;
  private progress: ReviewProgress | undefined;
  private current: number | undefined;
  // Nodes are rebuilt per progress message; reveal() needs the instances the tree holds.
  private files: Node[] = [];
  private byIndex = new Map<number, Node>();

  constructor(
    private readonly repoPath: () => string | undefined,
    private readonly reading: () => { filePath: string; status: string } | undefined,
  ) {}

  refresh(): void {
    this.changes.fire();
  }

  setProgress(progress: ReviewProgress | undefined): void {
    this.progress = progress;
    this.files = [];
    this.byIndex.clear();
    for (const file of progress?.files ?? []) {
      this.files.push({ kind: "file", file });
      for (const hunk of file.hunks) this.byIndex.set(hunk.index, { kind: "hunk", hunk, file });
    }
    this.changes.fire();
  }

  setCurrent(index: number | undefined): void {
    this.current = index;
    this.changes.fire();
  }

  currentNode(): Node | undefined {
    return this.current === undefined ? undefined : this.byIndex.get(this.current);
  }

  getChildren(node?: Node): Node[] {
    if (!node) return this.progress ? this.files : [];
    if (node.kind === "file") return node.file.hunks.flatMap((h) => this.byIndex.get(h.index) ?? []);
    return [];
  }

  getParent(node: Node): Node | undefined {
    return node.kind === "hunk" ? this.files.find((f) => f.kind === "file" && f.file === node.file) : undefined;
  }

  getTreeItem(node: Node): vscode.TreeItem {
    if (node.kind === "file") {
      const { file } = node;
      const item = new vscode.TreeItem(path.posix.basename(file.file_path), vscode.TreeItemCollapsibleState.Expanded);
      item.id = `file:${file.file_path}`;
      const dir = path.posix.dirname(file.file_path);
      item.description = `${dir === "." ? "" : `${dir}  `}${file.reviewed_count}/${file.hunk_count}`;
      const root = this.repoPath();
      if (root) item.resourceUri = vscode.Uri.file(path.join(root, file.file_path));
      // Markdown that still exists gets Preview and Read Aloud on its row.
      const markdown = /\.(md|markdown)$/i.test(file.file_path);
      const deleted = file.hunks.some((h) => fileChange(h.header) === "deleted");
      const read = this.reading();
      item.contextValue =
        markdown && !deleted
          ? read?.filePath === file.file_path
            ? `file.markdown.${read.status}`
            : "file.markdown"
          : "file";
      return item;
    }
    const { hunk } = node;
    const isCurrent = hunk.index === this.current;
    const item = new vscode.TreeItem(hunkLabel(hunk.header), vscode.TreeItemCollapsibleState.None);
    item.id = `hunk:${hunk.index}`;
    item.description = hunk.reviewed ? "reviewed" : undefined;
    item.tooltip = hunk.header;
    item.iconPath = hunk.reviewed
      ? new vscode.ThemeIcon("pass-filled", new vscode.ThemeColor("testing.iconPassed"))
      : new vscode.ThemeIcon(isCurrent ? "arrow-right" : "circle-large-outline");
    item.contextValue = isCurrent ? "hunk.current" : "hunk";
    item.command = { command: "pearReview.jumpToHunk", title: "Go to change", arguments: [hunk.index] };
    return item;
  }

  dispose(): void {
    this.changes.dispose();
  }
}
