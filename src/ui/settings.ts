import * as vscode from "vscode";

import type { Backend } from "../backend/backend.ts";
import type { Settings } from "../backend/protocol.ts";
import { showError } from "../log.ts";

// The backend's settings panel as two pickers. Saved the backend's way (set_settings,
// per repo in .review/ui_settings.json), so the web app sees the same choices.

const AGENT_LABELS: Record<string, string> = {
  none: "None — Act Now and Look deeper are off",
  cline: "Cline",
};

const PROVIDERS = [
  { id: "ollama", label: "Ollama", detail: "A local model" },
  { id: "anthropic", label: "Anthropic", detail: "Claude, through the API (needs Pear Review: Set Anthropic API Key)" },
];

export function register(backend: Backend): vscode.Disposable[] {
  const fetchSettings = async (provider?: string): Promise<Settings> => {
    const reply = backend.next("settings");
    backend.send("get_settings", provider ? { provider } : {});
    return reply;
  };

  const save = async (settings: Record<string, unknown>): Promise<Settings> => {
    const reply = backend.next("settings");
    backend.send("set_settings", { settings });
    return reply;
  };

  const requireBackend = (): boolean => {
    if (backend.state === "ready") return true;
    showError("Start a review first: settings are saved per repository, by the review backend.");
    return false;
  };

  const chooseAgent = async (): Promise<void> => {
    if (!requireBackend()) return;
    const current = (await fetchSettings()).harness_settings;
    const picked = await vscode.window.showQuickPick(
      current.agents.map((agent) => ({
        label: AGENT_LABELS[agent] ?? agent,
        agent,
        description: agent === current.agent ? "current" : undefined,
        detail:
          agent !== "none" && current.model
            ? `Uses the model set in ${AGENT_LABELS[agent] ?? agent}: ${current.provider ?? ""} ${current.model}`.trim()
            : undefined,
      })),
      { title: "Coding agent for Act Now and Look deeper" },
    );
    if (!picked || picked.agent === current.agent) return;
    await save({ harness: { agent: picked.agent } });
    void vscode.window.showInformationMessage(
      `Pear Review: coding agent set to ${AGENT_LABELS[picked.agent] ?? picked.agent}.`,
    );
  };

  const chooseModel = async (): Promise<void> => {
    if (!requireBackend()) return;
    const current = await fetchSettings();
    const provider = await vscode.window.showQuickPick(
      PROVIDERS.map((p) => ({ ...p, description: p.id === current.settings.provider ? "current" : undefined })),
      { title: "Model provider for the reviewer" },
    );
    if (!provider) return;
    const listing = await fetchSettings(provider.id);
    const section = listing.settings[provider.id] as { model?: string } | undefined;
    const typeOwn = "Type a model name…";
    const choice = await vscode.window.showQuickPick(
      [
        ...(listing.installed_models ?? []).map((model) => ({
          label: model,
          description: model === section?.model ? "current" : undefined,
        })),
        { label: typeOwn, description: section?.model ? `current: ${section.model}` : undefined },
      ],
      { title: `${provider.label} model` },
    );
    if (!choice) return;
    const model =
      choice.label === typeOwn
        ? await vscode.window.showInputBox({ title: `${provider.label} model`, value: section?.model ?? "" })
        : choice.label;
    if (!model?.trim()) return;
    const saved = await save({ provider: provider.id, [provider.id]: { model: model.trim() } });
    // Conversation settings apply per connection; a new one picks them up now.
    if (saved.applies_on_reconnect) await backend.reconnect();
    void vscode.window.showInformationMessage(`Pear Review: the reviewer now uses ${provider.label} ${model.trim()}.`);
  };

  const run = (fn: () => Promise<void>) => () =>
    fn().catch((err: unknown) => showError(err instanceof Error ? err.message : String(err)));

  return [
    vscode.commands.registerCommand("pearReview.chooseAgent", run(chooseAgent)),
    vscode.commands.registerCommand("pearReview.chooseModel", run(chooseModel)),
  ];
}
