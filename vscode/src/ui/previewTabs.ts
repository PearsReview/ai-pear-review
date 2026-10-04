import * as path from "node:path";
import * as vscode from "vscode";

import { labelNames } from "../review/previewLabel.ts";

// Which file a markdown preview tab shows. VS Code tells extensions nothing about it: the
// tab is a webview whose only clue is its label, "Preview README.md" in English and
// translated elsewhere. So a file matches when the label ends with its name, and a
// match is used only when it is the only one: two README.md files must not be confused.
// Previews this extension opens are remembered by URI, which settles most cases exactly.

const opened: vscode.Uri[] = [];

// Opens VS Code's markdown preview of a file, remembering which file it shows.
export function showPreview(uri: vscode.Uri): Thenable<unknown> {
  const at = opened.findIndex((u) => u.toString() === uri.toString());
  if (at >= 0) opened.splice(at, 1);
  opened.push(uri);
  return vscode.commands.executeCommand("markdown.showPreview", uri);
}

export function isPreviewTab(tab: vscode.Tab | undefined): tab is vscode.Tab {
  return tab?.input instanceof vscode.TabInputWebview && tab.input.viewType.includes("markdown.preview");
}

// The markdown preview a title-bar button was pressed on. The active group's tab if it
// is one; otherwise any visible group's, because a preview open beside another editor
// isn't always the active group when its title bar is clicked. A markdown file active
// in a text editor takes precedence over a preview elsewhere.
export function previewTab(): vscode.Tab | undefined {
  const active = vscode.window.tabGroups.activeTabGroup.activeTab;
  if (isPreviewTab(active)) return active;
  const text = vscode.window.activeTextEditor?.document;
  if (text && text.uri.scheme === "file" && text.languageId === "markdown") return undefined;
  return vscode.window.tabGroups.all.map((g) => g.activeTab).find(isPreviewTab);
}

// Whether a preview that could be this file's is on screen. A false positive only costs
// a preview refresh, so the label alone decides.
export function previewShowing(fsPath: string): boolean {
  const name = path.basename(fsPath);
  return vscode.window.tabGroups.all.some((g) => isPreviewTab(g.activeTab) && labelNames(g.activeTab.label, name));
}

// The file a preview tab shows, or undefined when it can't be told apart from another.
// Looked for in order: previews this extension opened (newest first), open documents,
// open text tabs, then the workspace's markdown files.
export async function previewFile(tab: vscode.Tab): Promise<vscode.Uri | undefined> {
  const matches = (uri: vscode.Uri | undefined): uri is vscode.Uri =>
    !!uri && uri.scheme === "file" && labelNames(tab.label, path.basename(uri.fsPath));
  const remembered = [...opened].reverse().find(matches);
  if (remembered) return remembered;
  const tiers: (() => Thenable<vscode.Uri[]> | vscode.Uri[])[] = [
    () => vscode.workspace.textDocuments.map((d) => d.uri),
    () =>
      vscode.window.tabGroups.all
        .flatMap((g) => g.tabs)
        .map((t) => (t.input instanceof vscode.TabInputText ? t.input.uri : undefined))
        .filter((u): u is vscode.Uri => !!u),
    () => vscode.workspace.findFiles("**/*.{md,markdown}", "**/node_modules/**", 5000),
  ];
  for (const tier of tiers) {
    const found = unique((await tier()).filter(matches));
    if (found.length === 1) return found[0];
    if (found.length > 1) return undefined;
  }
  return undefined;
}

function unique(uris: vscode.Uri[]): vscode.Uri[] {
  const seen = new Map<string, vscode.Uri>();
  for (const uri of uris) seen.set(uri.toString(), uri);
  return [...seen.values()];
}
