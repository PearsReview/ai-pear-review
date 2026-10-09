import * as vscode from "vscode";

import type { Backend } from "../backend/backend.ts";
import type { Settings } from "../backend/protocol.ts";
import { output, showError } from "../log.ts";
import type { Prefs } from "./prefs.ts";

// The web app's settings panel, as a native menu opened from the chat's gear, the one
// way in. Model, speech and agent settings are saved the backend's way (set_settings,
// per repo in .review/ui_settings.json), so the web app sees the same choices; the
// three preferences are the extension's own (prefs.ts).

const AGENT_LABELS: Record<string, string> = {
  none: "None — Act Now and Look deeper are off",
  cline: "Cline",
};

const PROVIDERS = [
  { id: "ollama", label: "Ollama", detail: "A local model" },
  { id: "anthropic", label: "Anthropic", detail: "Claude, through the API (needs an Anthropic API key)" },
  {
    id: "openai",
    label: "OpenAI-compatible",
    detail: "Any OpenAI-compatible endpoint or proxy/gateway (LiteLLM, vLLM) — set a base URL and API key",
  },
];

const PREP_LABELS: Record<string, string> = {
  project_overview: "Project overview",
  changeset: "Change briefings",
};

interface MenuItem extends vscode.QuickPickItem {
  run?: () => void | Promise<void>;
  // Reads or saves the backend's settings, so the changes must be open first.
  needsBackend?: boolean;
  // Leaves the menu instead of reopening it.
  closes?: boolean;
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
    // The OpenAI-compatible provider needs a base URL before it can list what the
    // endpoint serves. Ask for it first (saving it applies to the live config), so the
    // model picker below is populated. The key is set separately — it's a secret that
    // reaches the backend in its environment at start (see the API key menu item).
    if (provider.id === "openai") {
      const existing = (await fetchSettings()).settings.openai?.base_url ?? "";
      const baseUrl = await vscode.window.showInputBox({
        title: "OpenAI-compatible base URL",
        value: existing,
        prompt:
          "The endpoint's base URL, including the version path it expects (e.g. http://localhost:6655/litellm/v1).",
        ignoreFocusOut: true,
      });
      if (baseUrl === undefined) return;
      if (baseUrl.trim() && baseUrl.trim() !== existing) {
        const saved = await save({ provider: "openai", openai: { base_url: baseUrl.trim() } });
        if (saved.applies_on_reconnect) await backend.reconnect();
      }
    }
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

  // The reply-token limit actually in force for the current provider: a provider
  // block's own value wins over the shared top-level one (see conversation_service).
  const providerMaxTokens = (s: Settings["settings"]): number | null | undefined => {
    const block = s.provider ? (s[s.provider] as { max_tokens?: number | null } | undefined) : undefined;
    return block?.max_tokens ?? s.max_tokens;
  };

  const modelLimits = async (current: Settings): Promise<void> => {
    const s = current.settings;
    const changes: Record<string, unknown> = {};
    if (s.provider === "ollama") {
      const numCtx = await askNumber(
        "Context size (tokens)",
        s.ollama?.num_ctx,
        "How much the local model can read at once.",
      );
      if (numCtx !== undefined) changes.ollama = { num_ctx: numCtx };
    }
    const maxTokens = await askNumber(
      "Max reply tokens",
      providerMaxTokens(s),
      "The longest reply the reviewer may write.",
    );
    if (maxTokens !== undefined) {
      // max_tokens resolves per-provider: a provider block's own value wins over the
      // shared top-level one (conversation_service), and anthropic/openai set theirs in
      // config.yaml — so a top-level write would be shadowed and do nothing for them.
      // Write it where that provider actually reads it; ollama has no own value and uses
      // the shared one.
      if (s.provider === "anthropic" || s.provider === "openai") {
        changes[s.provider] = { ...(changes[s.provider] as object | undefined), max_tokens: maxTokens };
      } else {
        changes.max_tokens = maxTokens;
      }
    }
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
      ignoreFocusOut: true,
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

  const modelSummary = (s?: Settings["settings"]): string =>
    s ? `${s.provider ?? "?"} · ${(s.provider && (s[s.provider] as { model?: string })?.model) || "?"}` : "";

  // The settings panel and its submenus: one loop that rebuilds its items after each
  // action (so toggles and summaries refresh) and reopens like a panel until Escape.
  // buildItems runs each iteration; it fetches live settings when it needs them.
  const runMenu = async (title: string, buildItems: () => Promise<MenuItem[]>): Promise<void> => {
    for (;;) {
      const live = backend.state === "ready";
      const picked = await vscode.window.showQuickPick(await buildItems(), {
        title,
        placeHolder: live
          ? "Choose a setting to change"
          : "Model, speech and agent settings appear once the changes are open",
      });
      if (!picked?.run) return;
      if (!live && picked.needsBackend) {
        if (!(await ensureBackend())) return;
        continue;
      }
      await picked.run();
      if (picked.closes) return;
    }
  };

  const llmMenu = (): Promise<void> =>
    runMenu("LLM", async () => {
      const live = backend.state === "ready";
      const current = live ? await fetchSettings() : undefined;
      const s = current?.settings;
      const p = prefs.values;
      return [
        {
          label: "$(hubot) Model",
          description: live ? modelSummary(s) : "open the changes to see",
          run: chooseModel,
          needsBackend: true,
        },
        {
          label: "$(settings) Limits",
          needsBackend: true,
          description: s
            ? [
                s.provider === "ollama" && s.ollama?.num_ctx ? `context ${s.ollama.num_ctx}` : "",
                providerMaxTokens(s) ? `reply ${providerMaxTokens(s)} tokens` : "",
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
        {
          label: "$(key) OpenAI-compatible API key",
          description: (await context.secrets.get("pearReview.openaiApiKey")) ? "set" : "not set",
          run: async () => {
            await vscode.commands.executeCommand("pearReview.setOpenaiApiKey");
          },
        },
        {
          label: "$(sparkle) Explain changes",
          description: p.autoNarrate ? "Automatically" : "When I ask",
          run: () => prefs.set({ autoNarrate: !p.autoNarrate }),
        },
      ];
    });

  const voiceMenu = (): Promise<void> =>
    runMenu("Voice", async () => {
      const live = backend.state === "ready";
      const current = live ? await fetchSettings() : undefined;
      const p = prefs.values;
      return [
        {
          label: "$(unmute) Speak replies aloud",
          description: onOff(p.tts),
          detail: "The speaker button on each message works either way.",
          run: () => prefs.set({ tts: !p.tts }),
        },
        { label: "$(mic) Voice input", description: onOff(p.stt), run: () => prefs.set({ stt: !p.stt }) },
        {
          label: "$(unmute) Text-to-speech service",
          needsBackend: true,
          description: current?.tts_settings?.endpoint ?? "",
          run: async () => {
            if (current) await speechService("tts", current);
          },
        },
        {
          label: "$(mic) Speech-to-text service",
          needsBackend: true,
          description: current?.stt_settings?.endpoint ?? "",
          run: async () => {
            if (current) await speechService("stt", current);
          },
        },
      ];
    });

  const openMenu = (): Promise<void> =>
    runMenu("Pear Review settings", async () => {
      const live = backend.state === "ready";
      const current = live ? await fetchSettings() : undefined;
      const harness = current?.harness_settings;
      return [
        {
          label: "$(book) Get started",
          description: "a walkthrough of Pear Review",
          run: () => void vscode.commands.executeCommand("pearReview.getStarted"),
          closes: true,
        },
        {
          label: "$(hubot) LLM",
          description: live ? modelSummary(current?.settings) : "model, limits, API keys",
          run: () => llmMenu(),
        },
        {
          label: "$(unmute) Voice",
          description: "narration, speech services",
          run: () => voiceMenu(),
        },
        {
          label: "$(tools) Coding agent",
          needsBackend: true,
          description: harness
            ? `${AGENT_LABELS[harness.agent] ?? harness.agent}${harness.agent !== "none" && harness.model ? ` · ${harness.model}` : ""}`
            : "",
          run: () => chooseAgent(),
        },
        {
          label: "$(key) Coding agent API key",
          description: (await context.secrets.get("pearReview.agentApiKey"))
            ? "set"
            : "not set — uses the key saved by `cline auth`",
          detail: "For Act Now and Look deeper. The Cline extension's own key doesn't reach them.",
          run: async () => {
            await vscode.commands.executeCommand("pearReview.setAgentApiKey");
          },
        },
        {
          label: "$(book) Review context",
          needsBackend: true,
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
          closes: true,
        },
      ];
    });

  const run = (fn: (arg?: unknown) => Promise<void>) => (arg?: unknown) =>
    fn(arg).catch((err: unknown) => showError(err));

  return [
    vscode.commands.registerCommand("pearReview.settings", run(openMenu)),
    vscode.commands.registerCommand("pearReview.getStarted", () =>
      vscode.commands.executeCommand("workbench.action.openWalkthrough", `${context.extension.id}#getStarted`, false),
    ),
    vscode.commands.registerCommand("pearReview.chooseAgent", run(chooseAgent)),
    vscode.commands.registerCommand("pearReview.chooseModel", run(chooseModel)),
  ];
}
