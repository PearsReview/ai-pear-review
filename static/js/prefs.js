// Reviewer preferences that persist in localStorage and the controls that
// set them: the light/dark theme override, the mic/speech on-off toggles,
// whether changes are explained automatically, and the merged/split diff
// view mode.
//
// Distinct from settings.js, which saves to the server: everything here is
// per-browser and takes effect immediately. The voice prefs are additionally
// mirrored to the server (see sendVoicePrefs) so it can skip a real TTS call
// rather than synthesising audio the browser will not play.

import { state } from "./state.js";
import { AUTO_NARRATE_KEY, send } from "./ws.js";
import { renderCodeView, rerenderCurrentView } from "./code-view.js";
import { micBtn } from "./dom.js";
import { stopAllAudio } from "./audio.js";
import { updateExplainBtn } from "./explain.js";

// This module's own controls. They live here rather than in dom.js
// because nothing else touches them — dom.js is for elements more than
// one module reaches for.
const themeToggleBtn = document.getElementById("theme-toggle-btn");
const themeToggleIcon = document.getElementById("theme-toggle-icon");
const sttPrefRadios = document.querySelectorAll('input[name="stt-pref"]');
const ttsPrefRadios = document.querySelectorAll('input[name="tts-pref"]');
const narratePrefRadios = document.querySelectorAll('input[name="narrate-pref"]');
const voiceSettingsBtn = document.getElementById("voice-settings-btn");
const voicePrefsPanel = document.getElementById("voice-prefs-panel");
const viewMergedBtn = document.getElementById("view-merged-btn");
const viewSplitBtn = document.getElementById("view-split-btn");
// --- Theme toggle (manual override of prefers-color-scheme; see style.css's
// :root[data-theme] rules) ---
const THEME_KEY = "ai_pear_review_theme";

function applyTheme(theme) {
  // theme is "light" | "dark" | null (null = follow the OS, no attribute set)
  if (theme === "light" || theme === "dark") {
    document.documentElement.setAttribute("data-theme", theme);
  } else {
    document.documentElement.removeAttribute("data-theme");
  }
  themeToggleIcon.setAttribute("href", theme === "dark" ? "#icon-sun" : theme === "light" ? "#icon-moon" : "#icon-monitor");
  themeToggleBtn.title = theme === "dark"
    ? "Dark theme — click for light"
    : theme === "light"
    ? "Light theme — click to follow system"
    : "Following system theme — click for dark";
  // A 3-state cycle doesn't fit aria-pressed well — mirroring the dynamic
  // title into aria-label at least keeps the current-state announcement
  // in sync for assistive tech, same as every icon-only button below.
  themeToggleBtn.setAttribute("aria-label", themeToggleBtn.title);
}

export function initTheme() {
  let saved = null;
  try {
    saved = localStorage.getItem(THEME_KEY);
  } catch {
    // non-critical, ignore
  }
  applyTheme(saved);
}

// Cycles light -> dark -> system -> light..., same three-state idea as most
// editors' theme toggles, rather than a plain on/off that can never get back
// to "just follow the OS".
themeToggleBtn.addEventListener("click", () => {
  const current = document.documentElement.getAttribute("data-theme");
  const next = current === "light" ? "dark" : current === "dark" ? null : "light";
  applyTheme(next);
  try {
    if (next) localStorage.setItem(THEME_KEY, next);
    else localStorage.removeItem(THEME_KEY);
  } catch {
    // non-critical, ignore
  }
});
initTheme();

// --- Voice input/output preferences (STT/TTS radio buttons) ---
const VOICE_PREFS_KEY = "ai_pear_review_voice_prefs";

export function initVoicePrefs() {
  let saved = null;
  try {
    saved = JSON.parse(localStorage.getItem(VOICE_PREFS_KEY) || "null");
  } catch {
    saved = null;
  }
  if (saved && typeof saved === "object") {
    if (typeof saved.stt === "boolean") state.sttPrefOn = saved.stt;
    if (typeof saved.tts === "boolean") state.ttsPrefOn = saved.tts;
  }
  for (const r of sttPrefRadios) r.checked = (r.value === "on") === state.sttPrefOn;
  for (const r of ttsPrefRadios) r.checked = (r.value === "on") === state.ttsPrefOn;
  updateMicAvailability();
}

function saveVoicePrefs() {
  try {
    localStorage.setItem(VOICE_PREFS_KEY, JSON.stringify({ stt: state.sttPrefOn, tts: state.ttsPrefOn }));
  } catch {
    // non-critical, ignore
  }
}

// Mirrors the preference to the server so it can skip the real STT/TTS
// HTTP calls when off, not just have the browser stay silent after the
// server already paid for them. Sent on every change, and once right after
// the socket opens (see below) so the very first hunk's narration respects
// a preference saved from a previous session.
export function sendVoicePrefs() {
  send("set_voice_prefs", { stt_enabled: state.sttPrefOn, tts_enabled: state.ttsPrefOn });
}

export function updateMicAvailability() {
  micBtn.disabled = !state.sttAvailable || !state.sttPrefOn;
  micBtn.title = !state.sttPrefOn
    ? "Voice input turned off — see Mic preference"
    : state.sttAvailable
    ? "Push to talk"
    : "Voice input unavailable — type your reply";
  micBtn.setAttribute("aria-label", micBtn.title);
  // An already-open inline comment composer bakes mic availability into its
  // own rendered markup (see renderComposerHtml) rather than keeping a
  // second live DOM node in sync — re-render so it picks up a live change.
  rerenderCurrentView();
}

for (const r of sttPrefRadios) {
  r.addEventListener("change", () => {
    state.sttPrefOn = document.querySelector('input[name="stt-pref"]:checked').value === "on";
    updateMicAvailability();
    saveVoicePrefs();
    sendVoicePrefs();
  });
}
for (const r of ttsPrefRadios) {
  r.addEventListener("change", () => {
    state.ttsPrefOn = document.querySelector('input[name="tts-pref"]:checked').value === "on";
    // Turning speech off should stop speech. The state.ttsPrefOn guard only gates
    // *new* clips being queued, so without this a turn already split into
    // several would keep talking through clips 2..N after the reviewer
    // switched it off.
    if (!state.ttsPrefOn) stopAllAudio();
    saveVoicePrefs();
    sendVoicePrefs();
  });
}
initVoicePrefs();

// --- When changes are explained (automatically, or via the Explain button) ---
// Per-browser like the voice prefs. The server learns it from the socket URL
// on connect (see connectionQuery in ws.js) and from "set_narration_prefs"
// on every change; a change applies from the next hunk moved to.
function initNarratePref() {
  try {
    // Only an explicit "1" turns it on — on request is the default.
    state.autoNarrate = localStorage.getItem(AUTO_NARRATE_KEY) === "1";
  } catch {
    // non-critical, ignore
  }
  for (const r of narratePrefRadios) r.checked = (r.value === "auto") === state.autoNarrate;
}

for (const r of narratePrefRadios) {
  r.addEventListener("change", () => {
    state.autoNarrate = document.querySelector('input[name="narrate-pref"]:checked').value === "auto";
    try {
      localStorage.setItem(AUTO_NARRATE_KEY, state.autoNarrate ? "1" : "0");
    } catch {
      // non-critical, ignore
    }
    send("set_narration_prefs", { auto_narrate: state.autoNarrate });
    updateExplainBtn();
  });
}
initNarratePref();

// The radio buttons themselves stay tucked away behind a settings toggle
// (most reviewers never touch these) — same collapsed-by-default,
// localStorage-remembered pattern as the files sidebar.
const VOICE_SETTINGS_OPEN_KEY = "ai_pear_review_voice_settings_open";

export function setVoiceSettingsOpen(open) {
  voicePrefsPanel.classList.toggle("hidden", !open);
  voiceSettingsBtn.classList.toggle("active", open);
  voiceSettingsBtn.setAttribute("aria-expanded", String(open));
  try {
    localStorage.setItem(VOICE_SETTINGS_OPEN_KEY, open ? "1" : "0");
  } catch {
    // non-critical, ignore
  }
}

voiceSettingsBtn.addEventListener("click", () => {
  setVoiceSettingsOpen(voicePrefsPanel.classList.contains("hidden"));
});
try {
  if (localStorage.getItem(VOICE_SETTINGS_OPEN_KEY) === "1") setVoiceSettingsOpen(true);
} catch {
  // non-critical, ignore
}

// --- Merged/Split diff view toggle ---
const DIFF_VIEW_MODE_KEY = "ai_pear_review_diff_view_mode";

export function setDiffViewMode(mode) {
  state.diffViewMode = mode;
  viewMergedBtn.classList.toggle("active", mode === "merged");
  viewSplitBtn.classList.toggle("active", mode === "split");
  viewMergedBtn.setAttribute("aria-pressed", String(mode === "merged"));
  viewSplitBtn.setAttribute("aria-pressed", String(mode === "split"));
  try {
    localStorage.setItem(DIFF_VIEW_MODE_KEY, mode);
  } catch {
    // non-critical, ignore
  }
  // Re-render whatever's already on screen in the new mode — no server
  // round-trip needed, the full content is already in state.lastRenderedView.
  if (state.lastRenderedView) {
    renderCodeView(state.lastRenderedView.fullLines, state.lastRenderedView.highlightStart, state.lastRenderedView.highlightEnd, state.lastRenderedView.filePath);
  }
}

export function initDiffViewMode() {
  let saved = null;
  try {
    saved = localStorage.getItem(DIFF_VIEW_MODE_KEY);
  } catch {
    // non-critical, ignore
  }
  state.diffViewMode = saved === "split" ? "split" : "merged";
  viewMergedBtn.classList.toggle("active", state.diffViewMode === "merged");
  viewSplitBtn.classList.toggle("active", state.diffViewMode === "split");
  viewMergedBtn.setAttribute("aria-pressed", String(state.diffViewMode === "merged"));
  viewSplitBtn.setAttribute("aria-pressed", String(state.diffViewMode === "split"));
}
viewMergedBtn.addEventListener("click", () => setDiffViewMode("merged"));
viewSplitBtn.addEventListener("click", () => setDiffViewMode("split"));
initDiffViewMode();
