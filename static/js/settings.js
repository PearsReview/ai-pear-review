// The model settings panel: provider/model/limits, the TTS and STT
// endpoints, and the coding-agent picker, plus the review-context freshness
// list underneath them.
//
// Owns every #setting-* and #model-settings-* element. Changes are saved
// server-side (see app/services/settings_store.py), not to localStorage —
// these decide what the *next* connection's ConversationClient is built
// with, unlike the voice prefs.

import { transcriptEl } from "./dom.js";

import { send } from "./ws.js";
import { clearThinking } from "./transcript.js";
// --- Model settings (provider/model/context/limits) ---
// Server-side state, unlike the voice prefs above: these decide what the
// *next* connection's ConversationClient is built with, so they're saved
// to disk by the server (see app/services/settings_store.py) rather than
// to localStorage, and re-read on the next connect.
const modelSettingsBtn = document.getElementById("model-settings-btn");
const modelSettingsPanel = document.getElementById("model-settings-panel");
const settingProvider = document.getElementById("setting-provider");
const settingModel = document.getElementById("setting-model");
const settingNumCtx = document.getElementById("setting-num-ctx");
const settingNumCtxRow = document.getElementById("setting-num-ctx-row");
const settingMaxTokens = document.getElementById("setting-max-tokens");
const settingMaxTokensRow = document.getElementById("setting-max-tokens-row");
const settingTimeout = document.getElementById("setting-timeout");
const settingTtsEndpoint = document.getElementById("setting-tts-endpoint");
const settingTtsToken = document.getElementById("setting-tts-token");
const settingSttEndpoint = document.getElementById("setting-stt-endpoint");
const settingSttToken = document.getElementById("setting-stt-token");
const settingHarnessAgent = document.getElementById("setting-harness-agent");
const settingSaveBtn = document.getElementById("setting-save-btn");
const settingStatus = document.getElementById("setting-status");

// Pulled out to a named function so the guided tour can open this same
// panel (rather than toggle it — see openModelSettingsPanel's own callers)
// when the reviewer wants tour audio but the TTS endpoint isn't reachable,
// without duplicating the open-vs-toggle logic here.
export function openModelSettingsPanel() {
  modelSettingsPanel.classList.remove("hidden");
  modelSettingsBtn.setAttribute("aria-expanded", "true");
  // A fresh, explicit request for the truth — the opposite intent from
  // the provider <select>'s own refetch below, so it's the one place
  // (besides a save) that's allowed to turn suppression back off. See
  // suppressProviderSync's own comment for why this can't just reset
  // itself after any one response.
  suppressProviderSync = false;
  // Ask on open rather than caching: the effective values can have been
  // changed from another tab, and the installed-model list is a live
  // lookup against whatever Ollama currently has pulled.
  send("get_settings", {});
}

modelSettingsBtn.addEventListener("click", () => {
  if (modelSettingsPanel.classList.contains("hidden")) {
    openModelSettingsPanel();
  } else {
    modelSettingsPanel.classList.add("hidden");
    modelSettingsBtn.setAttribute("aria-expanded", "false");
  }
});

// Snapshot of the conversation fields as last loaded from the server —
// compared against on Save so the payload only carries "provider" (always
// present, since the dropdown always has *some* value selected) when the
// reviewer actually touched this section. Without it, saving the TTS
// endpoint alone would still re-send the unchanged provider, sanitize()
// would keep it (it's always in the allowlist), and the save ack would
// claim "reload the page to use these" for a save that changed nothing
// conversation-related at all.
let loadedConversationSnapshot = null;

// Set true by the provider <select>'s own "change" handler right before it
// asks for a re-fetch — see there for why: nothing has been saved yet at
// that point, so the server still reports whichever provider was last
// SAVED, and unconditionally writing that into settingProvider.value here
// would silently revert the reviewer's own click a moment later.
//
// Deliberately NOT cleared here after being applied once. get_settings
// responses are only guaranteed to arrive in the order they were SENT —
// if the panel's own opening fetch is still in flight (installed-models
// lookup has real, variable latency) when the reviewer picks a provider,
// that first response can land AFTER the flag was set, "consume" it, and
// leave a second, still-unsaved response free to revert the pick anyway.
// Only openModelSettingsPanel (a genuinely fresh ask) and a save (whose
// ack now reflects the truth) are allowed to turn this back off — see
// both.
let suppressProviderSync = false;

// Compared on Save, same reason as loadedConversationSnapshot: only send the
// agent when the reviewer actually changed it.
let loadedHarnessAgent = null;

function setModelOptions(names, current) {
  // The saved model is always offered, even when the provider couldn't list
  // it (no API key yet, Ollama unreachable, or a model since removed).
  // Dropping it would make the panel display — and the next save write — a
  // model nobody picked, silently switching which model the review runs on.
  const values = current && !names.includes(current) ? [current, ...names] : names;
  settingModel.replaceChildren();
  for (const name of values) {
    const option = document.createElement("option");
    option.value = name;
    option.textContent = name;
    settingModel.appendChild(option);
  }
  settingModel.value = current;
  // Nothing listed and nothing saved — an empty dropdown is a dead control,
  // so say so by disabling it rather than offering an empty menu.
  settingModel.disabled = values.length === 0;
}

export function onSettings(payload) {
  const s = payload.settings || {};
  // Whatever this response is (a fetch, or a save's own ack), the panel now
  // matches the server again — see the provider <select>'s handler for what
  // saving before this point used to write.
  settingSaveBtn.disabled = false;
  if (!suppressProviderSync) {
    settingProvider.value = s.provider || "ollama";
  }
  // Whichever provider is actually showing in the dropdown right now —
  // NOT necessarily s.provider (see suppressProviderSync above). Every
  // provider-dependent field below follows this, so picking a provider
  // and not yet saving still looks right: the correct model shows, and
  // context-size/max-tokens grey out immediately rather than only after
  // a save+reopen round trip.
  const provider = settingProvider.value;
  const isAnthropic = provider === "anthropic";
  settingTimeout.value = s.timeout_seconds ?? "";
  const providerSettings = isAnthropic ? s.anthropic || {} : s.ollama || {};
  // The provider's own cap wins over the shared one — Anthropic needs a
  // bigger budget than Ollama because the model's reasoning spends it too
  // (see config.yaml's anthropic.max_tokens note).
  settingMaxTokens.value = providerSettings.max_tokens ?? s.max_tokens ?? "";
  // Options before value: a <select> silently drops a value it has no
  // option for, which would leave the box showing the first model in the
  // list while the server still had a different one saved.
  setModelOptions(payload.installed_models || [], providerSettings.model || "");
  settingNumCtx.value = (s.ollama || {}).num_ctx ?? "";
  // What the SERVER last saved — deliberately NOT what's on screen. With
  // suppressProviderSync set, the dropdown already shows an unsaved pick,
  // and snapshotting that had Save comparing the pick against itself:
  // conversationChanged came out false, "provider" was never sent, and the
  // ack's real (still-ollama) settings then flipped the dropdown back. The
  // symptom was that switching to Anthropic from the panel could not be
  // saved at all — Save appeared to work and silently reverted.
  const savedProvider = s.provider || "ollama";
  const savedProviderSettings = savedProvider === "anthropic" ? s.anthropic || {} : s.ollama || {};
  loadedConversationSnapshot = {
    provider: savedProvider,
    maxTokens: String(savedProviderSettings.max_tokens ?? s.max_tokens ?? ""),
    timeout: settingTimeout.value,
    model: savedProviderSettings.model || "",
    numCtx: settingNumCtx.value,
  };
  // Context size stays Ollama-only — Anthropic manages its own window, so
  // there is nothing to unlock and a greyed-out-but-visible box would read
  // as "you can't touch this right now", which is the wrong message. Hidden
  // AND disabled, as a second guard against the hidden input still being
  // reachable some other way (a stray Tab, a test poking the DOM).
  //
  // Max reply tokens is NOT Ollama-only any more. It used to be withheld on
  // the grounds that Anthropic should keep config.yaml's default, but that
  // default is 300 — sized for a local model's spoken turn, and shared with
  // the model's own reasoning on Anthropic, so it truncated replies
  // mid-sentence and sometimes returned no text at all.
  settingNumCtxRow.classList.toggle("hidden", isAnthropic);
  settingNumCtx.disabled = isAnthropic;
  settingMaxTokensRow.classList.remove("hidden");
  settingMaxTokens.disabled = false;

  // Only get_settings (and a set_settings ack that touched tts/stt)
  // carries these — guarded the same way context_status is below, so
  // saving just the conversation half doesn't blank a value the reviewer
  // hasn't touched. Token fields are never pre-filled with the real
  // secret (the server never sends it back, see effective_tts_settings) —
  // only the placeholder hints whether one is already saved.
  if (payload.tts_settings) {
    settingTtsEndpoint.value = payload.tts_settings.endpoint || "";
    settingTtsToken.value = "";
    settingTtsToken.placeholder = payload.tts_settings.token_set
      ? "•••••••• saved — leave blank to keep it"
      : "";
  }
  if (payload.stt_settings) {
    settingSttEndpoint.value = payload.stt_settings.endpoint || "";
    settingSttToken.value = "";
    settingSttToken.placeholder = payload.stt_settings.token_set
      ? "•••••••• saved — leave blank to keep it"
      : "";
  }
  if (payload.harness_settings) {
    settingHarnessAgent.value = payload.harness_settings.agent || "none";
    loadedHarnessAgent = settingHarnessAgent.value;
    const harness = payload.harness_settings;
    document.getElementById("setting-harness-model").textContent =
      harness.model ? `${harness.provider} · ${harness.model}` : "not set";
    document.getElementById("setting-harness-model-source").textContent =
      harness.model_source === "config.yaml" ? "Pinned in app/config.yaml (harness.cline.env)"
      : harness.model_source === "Cline settings" ? "From your Cline settings"
      : "Cline isn't set up yet";
    document.getElementById("setting-harness-note").textContent = harness.research_note || "";
    // Nothing to show while Act Now is off.
    document.getElementById("setting-harness-model-row").classList.toggle("hidden", loadedHarnessAgent === "none");
  }
  if (payload.saved) {
    // tts/stt apply immediately (see handle_set_settings's own docstring —
    // both are true singletons, not per-connection like the conversation
    // client), so they get their own messages rather than being folded
    // into the reload-required one below when several were saved together.
    const parts = [];
    if (payload.applies_on_reconnect) parts.push("Model settings saved — reload the page to use these.");
    if (payload.tts_applied_now) parts.push("TTS settings updated — try it now.");
    if (payload.stt_applied_now) parts.push("STT settings updated — try it now.");
    if (payload.harness_applied_now) parts.push("Coding agent updated — Act Now uses it now.");
    settingStatus.textContent = parts.join(" ") || "Saved.";
  }
  // Only get_settings carries this; the set_settings ack doesn't. Guarded
  // so saving a model change doesn't blank the panel's context section.
  if (payload.context_status) renderContextStatus(payload.context_status);
}

const CONTEXT_FILE_LABELS = {
  project_overview: "Project overview",
  call_map: "Call map",
  changeset: "Change themes",
};

function renderContextStatus(status) {
  const list = document.getElementById("context-files-list");
  if (!list) return;
  list.replaceChildren();

  for (const [key, label] of Object.entries(CONTEXT_FILE_LABELS)) {
    const info = status[key];
    if (!info) continue;

    const item = document.createElement("li");
    const name = document.createElement("strong");
    name.textContent = label;
    item.appendChild(name);

    const stateEl = document.createElement("span");
    if (!info.present) {
      stateEl.className = "context-files-state missing";
      stateEl.textContent = "not generated";
    } else if (info.head_moved) {
      // Worth distinguishing from "missing": stale prep is still used, and
      // for an overview it's usually still true. Nothing here is a warning.
      stateEl.className = "context-files-state stale";
      stateEl.textContent = `written ${formatWhen(info.generated_at)}, repo has moved since`;
    } else {
      stateEl.className = "context-files-state fresh";
      stateEl.textContent = `written ${formatWhen(info.generated_at)}, up to date`;
    }
    item.appendChild(stateEl);

    if (info.refresh_hint) {
      const hint = document.createElement("code");
      hint.className = "context-files-hint";
      hint.textContent = info.refresh_hint;
      item.appendChild(hint);
    }
    list.appendChild(item);
  }
}

function formatWhen(iso) {
  if (!iso) return "at an unknown time";
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return "at an unknown time";
  const days = Math.floor((Date.now() - then) / 86400000);
  if (days <= 0) return "today";
  if (days === 1) return "yesterday";
  return `${days} days ago`;
}

export function onContextTooLarge(payload) {
  // Not an error and not a silent truncation: the file genuinely doesn't
  // fit the configured window, so rather than sending it anyway and
  // letting the provider drop the front (see config.yaml's num_ctx note),
  // offer the two things that actually work — take it to a model with room,
  // or narrow the question down to specific lines.
  clearThinking();
  const wrap = document.createElement("div");
  wrap.className = "turn system context-too-large";
  const isHunk = payload.kind === "hunk";
  const summary = document.createElement("div");
  const size =
    `about ${payload.estimated_tokens} tokens, over this model's ${payload.budget_tokens}-token budget`;
  if (!isHunk) {
    summary.textContent =
      `${payload.file_path} is ${size}. Nothing was sent — the whole file wouldn't have fit, ` +
      `and the part that got cut is chosen by the provider, not by you.`;
  } else if (payload.reason === "call_failed") {
    // The normal call (with the diff) failed and was retried from the
    // briefing alone — see _generate_narration/handle_reply.
    summary.textContent = payload.answered_from_briefing
      ? `The model couldn't answer from the full hunk of ${payload.file_path}, so what's said above ` +
        `comes from the hunk's briefing, not from the code itself.`
      : `The model couldn't answer about ${payload.file_path}, with or without the diff.`;
  } else if (payload.answered_from_briefing) {
    // A hunk too large to send is answered from its briefing instead (see
    // app/handlers/narration.py) — useful, but not read from the code, so
    // say so right under the answer rather than let it pass as grounded.
    summary.textContent =
      `This hunk of ${payload.file_path} is ${size}, so its diff wasn't sent. What's said above ` +
      `comes from the hunk's briefing, not from the code itself.`;
  } else {
    summary.textContent =
      `This hunk of ${payload.file_path} is ${size}, and it has no briefing to answer from, so ` +
      `nothing was sent. The prep-review skill writes one.`;
  }
  wrap.appendChild(summary);

  const actions = document.createElement("div");
  actions.className = "context-too-large-actions";

  const copyBtn = document.createElement("button");
  copyBtn.type = "button";
  copyBtn.textContent = "Copy question for Claude";
  copyBtn.title = "Copy this question, with its context, to ask elsewhere";
  copyBtn.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(payload.handoff_text);
      copyBtn.textContent = "Copied";
    } catch {
      // A blocked Clipboard API shouldn't mean the text is unreachable.
      window.prompt("Copy this into your own Claude Code session:", payload.handoff_text);
    }
  });
  actions.appendChild(copyBtn);

  const hint = document.createElement("span");
  hint.className = "context-too-large-hint";
  hint.textContent = isHunk
    ? "…or double-click the lines you mean and ask again — only your selection and the briefing are sent."
    : "…or double-click the lines you mean and ask again — a selection is sent instead of the whole file.";
  actions.appendChild(hint);

  wrap.appendChild(actions);
  transcriptEl.appendChild(wrap);
  transcriptEl.scrollTop = transcriptEl.scrollHeight;
}

settingSaveBtn.addEventListener("click", () => {
  const provider = settingProvider.value;
  const modelValue = settingModel.value.trim();
  const settings = {};

  // provider is always SOME value (the dropdown always has one selected),
  // so including it unconditionally would make sanitize() keep it every
  // single save (it's always in ALLOWED_CONVERSATION_KEYS) — and the ack
  // would then always claim "reload the page to use these" even for a
  // save that only touched the TTS endpoint below. Only send this section
  // at all when something in it actually differs from what was loaded.
  const currentSnapshot = {
    provider,
    maxTokens: settingMaxTokens.value,
    timeout: settingTimeout.value,
    model: modelValue,
    numCtx: settingNumCtx.value,
  };
  const conversationChanged =
    !loadedConversationSnapshot ||
    JSON.stringify(currentSnapshot) !== JSON.stringify(loadedConversationSnapshot);

  if (conversationChanged) {
    settings.provider = provider;
    settings.timeout_seconds = Number(settingTimeout.value) || undefined;
    if (provider === "ollama") {
      // Ollama keeps writing the SHARED max_tokens, which is what
      // config.yaml documents as its home; num_ctx is Ollama's own.
      settings.max_tokens = Number(settingMaxTokens.value) || undefined;
      settings.ollama = {};
      if (modelValue) settings.ollama.model = modelValue;
      if (Number(settingNumCtx.value)) settings.ollama.num_ctx = Number(settingNumCtx.value);
    } else {
      // Anthropic's cap goes in its own block, so raising it here can never
      // raise Ollama's too — the same reason model lives per provider.
      settings.anthropic = {};
      if (modelValue) settings.anthropic.model = modelValue;
      if (Number(settingMaxTokens.value)) settings.anthropic.max_tokens = Number(settingMaxTokens.value);
    }
  }

  // Separate sections of the same panel/Save button, not separate save
  // actions — see handle_set_settings server-side, which sanitises these
  // independently of the conversation fields above and applies them
  // immediately rather than on reconnect. Token fields are only included
  // when the reviewer actually typed something new — see sanitize_tts's
  // docstring for why a blank token field must never mean "clear it".
  const ttsEndpointValue = settingTtsEndpoint.value.trim();
  const ttsTokenValue = settingTtsToken.value;
  if (ttsEndpointValue || ttsTokenValue) {
    settings.tts = {};
    if (ttsEndpointValue) settings.tts.endpoint = ttsEndpointValue;
    if (ttsTokenValue) settings.tts.token = ttsTokenValue;
  }
  const sttEndpointValue = settingSttEndpoint.value.trim();
  const sttTokenValue = settingSttToken.value;
  if (sttEndpointValue || sttTokenValue) {
    settings.stt = {};
    if (sttEndpointValue) settings.stt.endpoint = sttEndpointValue;
    if (sttTokenValue) settings.stt.token = sttTokenValue;
  }

  if (loadedHarnessAgent !== null && settingHarnessAgent.value !== loadedHarnessAgent) {
    settings.harness = { agent: settingHarnessAgent.value };
  }

  // A save's own ack is trustworthy — its "settings" now genuinely
  // reflects what was just persisted (or, if conversationChanged was
  // false, whatever was already there), so this is the other place
  // (besides opening the panel) allowed to let onSettings sync
  // settingProvider.value again. See suppressProviderSync's own comment.
  suppressProviderSync = false;
  settingStatus.textContent = "Saving…";
  send("set_settings", { settings });
});

settingProvider.addEventListener("change", () => {
  // Re-fetch so the model box and the installed list follow the provider
  // rather than showing the previous one's values. suppressProviderSync
  // tells the response handler not to stomp the selection that just
  // triggered it: nothing has been saved yet, so the server still reports
  // whatever provider was last SAVED, and onSettings unconditionally
  // writing that back into settingProvider.value would silently revert
  // the reviewer's own click a moment later — a real bug, found by
  // actually watching the dropdown after selecting "anthropic" rather
  // than trusting the code: it flipped back to "ollama" client-side
  // before context-size/max-tokens ever got a chance to show as
  // disabled.
  suppressProviderSync = true;
  // Save is dead until the new provider's models arrive. Saving into that
  // gap wrote the OLD provider's model under the NEW provider's key — an
  // ollama model id saved as conversation.anthropic.model, which only shows
  // up later as a 404 from the Messages API. Reproduced twice: in this
  // repo's own .review/ui_settings.json, and by qa_agent's own settings
  // tests, which click Save immediately after switching.
  settingSaveBtn.disabled = true;
  // Name the picked provider: the server otherwise lists the SAVED one's
  // models, so switching to Anthropic offered Ollama's models and no Claude
  // model could be chosen at all (see handle_get_settings).
  send("get_settings", { provider: settingProvider.value });
});
