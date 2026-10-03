import * as vscode from "vscode";

import type { Backend } from "../backend/backend.ts";
import type { Settings } from "../backend/protocol.ts";
import { output, showError } from "../log.ts";
import type { Prefs } from "./prefs.ts";

// The web app's settings panel, as a native menu (Pear Review: Settings, the chat's
// gear). Model, speech and agent settings are saved the backend's way (set_settings,
// per repo in .review/ui_settings.json), so the web app sees the same choices; the
// three preferences are the extension's own (prefs.ts).

const AGENT_LABELS: Record<string, string> = {
  none: "None — Act Now and Look deeper are off",
  cline: "Cline",
};

const PROVIDERS = [
  { id: "ollama", label: "Ollama", detail: "A local model" },
  { id: "anthropic", label: "Anthropic", detail: "Claude, through the API (needs an Anthropic API key)" },
];

const PREP_LABELS: Record<string, string> = {
  project_overview: "Project overview",
  call_map: "Call map",
  changeset: "Change briefings",
};

interface MenuItem extends vscode.QuickPickItem {
  run?: () => void | Promise<void>;
}

export function register(context: vscode.ExtensionContext, backend: Backend, prefs: Prefs): vscode.Disposable[] {
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

  // Backend settings live in the repo under review, so the changes need to be open.
  const ensureBackend = async (): Promise<boolean> =>
    backend.state === "ready" || (await vscode.commands.executeCommand<boolean>("pearReview.openChanges")) === true;

  // `preset` (an agent id) skips the picker, for a keybinding or the tests.
  const chooseAgent = async (preset?: unknown): Promise<void> => {
    if (!(await ensureBackend())) return;
    const current = (await fetchSettings()).harness_settings;
    const given = typeof preset === "string" && current.agents.includes(preset) ? { agent: preset } : undefined;
    const picked =
      given ??
      (await vscode.window.showQuickPick(
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
      ));
    if (!picked || picked.agent === current.agent) return;
    await save({ harness: { agent: picked.agent } });
    void vscode.window.showInformationMessage(
      `Pear Review: coding agent set to ${AGENT_LABELS[picked.agent] ?? picked.agent}.`,
    );
  };

  const chooseModel = async (): Promise<void> => {
    if (!(await ensureBackend())) return;
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

  // A whole number, or undefined to leave it as it is.
  const askNumber = async (
    title: string,
    current: number | null | undefined,
    hint: string,
  ): Promise<number | undefined> => {
    const value = await vscode.window.showInputBox({
      title,
      prompt: hint,
      value: current === null || current === undefined ? "" : String(current),
      validateInput: (v) => (v.trim() === "" || /^\d+$/.test(v.trim()) ? undefined : "A whole number"),
    });
    return value?.trim() ? Number(value.trim()) : undefined;
  };

  const modelLimits = async (current: Settings): Promise<void> => {
    const s = current.settings;
    const changes: Record<string, unknown> = {};
    if (s.provider !== "anthropic") {
      const numCtx = await askNumber(
        "Context size (tokens)",
        s.ollama?.num_ctx,
        "How much the local model can read at once.",
      );
      if (numCtx !== undefined) changes.ollama = { num_ctx: numCtx };
    }
    const maxTokens = await askNumber("Max reply tokens", s.max_tokens, "The longest reply the reviewer may write.");
    if (maxTokens !== undefined) changes.max_tokens = maxTokens;
    const timeout = await askNumber("Timeout (seconds)", s.timeout_seconds, "How long one reply may take.");
    if (timeout !== undefined) changes.timeout_seconds = timeout;
    if (!Object.keys(changes).length) return;
    const saved = await save(changes);
    if (saved.applies_on_reconnect) await backend.reconnect();
  };

  const speechService = async (kind: "tts" | "stt", current: Settings): Promise<void> => {
    const name = kind === "tts" ? "Text-to-speech" : "Speech-to-text";
    const existing = kind === "tts" ? current.tts_settings : current.stt_settings;
    const endpoint = await vscode.window.showInputBox({
      title: `${name} endpoint`,
      value: existing?.endpoint ?? "",
      prompt: "The URL of the speech service.",
    });
    if (endpoint === undefined) return;
    const token = await vscode.window.showInputBox({
      title: `${name} API token (optional)`,
      password: true,
      prompt: existing?.token_set
        ? "A token is set. Leave empty to keep it."
        : "Only for a hosted service that needs one.",
    });
    if (token === undefined) return;
    const section: Record<string, string> = {};
    if (endpoint.trim()) section.endpoint = endpoint.trim();
    if (token.trim()) section.token = token.trim();
    if (Object.keys(section).length) await save({ [kind]: section });
  };

  const prepFiles = async (current: Settings): Promise<void> => {
    const status = current.context_status ?? {};
    const picked = await vscode.window.showQuickPick(
      Object.entries(status).map(([key, s]) => ({
        label: PREP_LABELS[key] ?? key,
        description: !s.present ? "missing" : s.head_moved ? "out of date" : "up to date",
        detail: s.refresh_hint,
        hint: s.refresh_hint,
      })),
      {
        title: "Review context: what the reviewer knows about the project",
        placeHolder: "Choose one to copy its refresh instruction",
      },
    );
    if (picked) {
      await vscode.env.clipboard.writeText(picked.hint.replace(/^Ask Claude Code: /, ""));
      void vscode.window.showInformationMessage(`Copied: ${picked.hint}`);
    }
  };

  const onOff = (on: boolean): string => (on ? "On" : "Off");

  const openMenu = async (): Promise<void> => {
    // Reopens after each change, like a settings panel, until Escape.
    for (;;) {
      const p = prefs.values;
      const live = backend.state === "ready";
      const current = live ? await fetchSettings() : undefined;
      const s = current?.settings;
      const model = s
        ? `${s.provider ?? "?"} · ${(s.provider && (s[s.provider] as { model?: string })?.model) || "?"}`
        : "";
      const harness = current?.harness_settings;
      const items: MenuItem[] = [
        { label: "Preferences", kind: vscode.QuickPickItemKind.Separator },
        {
          label: "$(sparkle) Explain changes",
          description: p.autoNarrate ? "Automatically" : "When I ask",
          run: () => prefs.set({ autoNarrate: !p.autoNarrate }),
        },
        {
          label: "$(unmute) Speak replies aloud",
          description: onOff(p.tts),
          detail: "The speaker button on each message works either way.",
          run: () => prefs.set({ tts: !p.tts }),
        },
        { label: "$(mic) Voice input", description: onOff(p.stt), run: () => prefs.set({ stt: !p.stt }) },
        { label: "Reviewer model", kind: vscode.QuickPickItemKind.Separator },
        { label: "$(hubot) Model", description: live ? model : "open the changes to see", run: chooseModel },
        {
          label: "$(settings) Limits",
          description: s
            ? [
                s.provider !== "anthropic" && s.ollama?.num_ctx ? `context ${s.ollama.num_ctx}` : "",
                s.max_tokens ? `reply ${s.max_tokens} tokens` : "",
                s.timeout_seconds ? `${s.timeout_seconds} s` : "",
              ]
                .filter(Boolean)
                .join(" · ")
            : "",
          run: async () => {
            if (current) await modelLimits(current);
          },
        },
        {
          label: "$(key) Anthropic API key",
          description: (await context.secrets.get("pearReview.anthropicApiKey")) ? "set" : "not set",
          run: async () => {
            await vscode.commands.executeCommand("pearReview.setAnthropicApiKey");
          },
        },
        { label: "Speech", kind: vscode.QuickPickItemKind.Separator },
        {
          label: "$(unmute) Text-to-speech service",
          description: current?.tts_settings?.endpoint ?? "",
          run: async () => {
            if (current) await speechService("tts", current);
          },
        },
        {
          label: "$(mic) Speech-to-text service",
          description: current?.stt_settings?.endpoint ?? "",
          run: async () => {
            if (current) await speechService("stt", current);
          },
        },
        { label: "Coding agent", kind: vscode.QuickPickItemKind.Separator },
        {
          label: "$(tools) Agent for Act Now and Look deeper",
          description: harness
            ? `${AGENT_LABELS[harness.agent] ?? harness.agent}${harness.agent !== "none" && harness.model ? ` · ${harness.model}` : ""}`
            : "",
          run: () => chooseAgent(),
        },
        { label: "Review context", kind: vscode.QuickPickItemKind.Separator },
        {
          label: "$(book) Prep files",
          description: current?.context_status
            ? Object.entries(current.context_status)
                .map(([k, v]) => `${PREP_LABELS[k] ?? k}: ${!v.present ? "missing" : v.head_moved ? "stale" : "ok"}`)
                .join(" · ")
            : "",
          run: async () => {
            if (current) await prepFiles(current);
          },
        },
        {
          label: "$(output) Show log",
          run: () => output.show(),
        },
      ];
      const picked = await vscode.window.showQuickPick(items, {
        title: "Pear Review settings",
        placeHolder: live
          ? "Choose a setting to change"
          : "Model, speech and agent settings appear once the changes are open",
      });
      if (!picked?.run) return;
      if (!live && picked.run !== undefined && /Model|Limits|service|Agent|Prep/.test(picked.label)) {
        if (!(await ensureBackend())) return;
        continue;
      }
      await picked.run();
      if (picked.label.includes("Show log")) return;
    }
  };

  const run = (fn: (arg?: unknown) => Promise<void>) => (arg?: unknown) =>
    fn(arg).catch((err: unknown) => showError(err instanceof Error ? err.message : String(err)));

  return [
    vscode.commands.registerCommand("pearReview.settings", run(openMenu)),
    vscode.commands.registerCommand("pearReview.chooseAgent", run(chooseAgent)),
    vscode.commands.registerCommand("pearReview.chooseModel", run(chooseModel)),
  ];
}
