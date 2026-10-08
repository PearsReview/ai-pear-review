// The guided tour: a spotlight overlay plus a step card, walked with
// Next/Back/Escape.
//
// Owns every #tour-* element and nothing else. Steps whose target isn't on
// screen are skipped rather than pointed at empty space (see
// isTourTargetVisible), so the same TOUR_STEPS list works before and after a
// review has started.
//
// Import-time work here is limited to reading its own localStorage flags and
// attaching listeners to its own elements — it never calls into the modules
// it imports while this module is evaluating, which is what keeps the cycle
// with the rest of the app safe.

import { state } from "./state.js";
import { send } from "./ws.js";
import { fileSidebarEl } from "./dom.js";
import { openModelSettingsPanel } from "./settings.js";
import { stopTourAudioQueue } from "./audio.js";
import { setFileSidebarOpen } from "./sidebar.js";

// --- Guided tour -----------------------------------------------------------
//
// A step-by-step spotlight over the real UI, not a slideshow of screenshots —
// every step points at the actual control, in whatever state it's in right
// now, so what's shown can never drift from what's really there. One
// highlight box and one card (see index.html) are reused across every step;
// nothing is rebuilt per step, just repositioned.
//
// Steps are written defensively on purpose: half the controls this tour
// talks about are conditionally visible (Start Review disappears once
// clicked, the file sidebar starts collapsed, Create plan only appears
// once something's queued). Rather than hand-track every one of those
// states, a step whose target isn't currently on screen is simply skipped —
// see isTourTargetVisible. That also means a step naturally reappears on a
// later run once its target exists (e.g. Create plan, once a comment has
// actually been queued).
const tourHighlightEl = document.getElementById("tour-highlight");
const tourCardEl = document.getElementById("tour-card");
const tourCardProgressEl = document.getElementById("tour-card-progress");
const tourCardTitleEl = document.getElementById("tour-card-title");
const tourCardBodyEl = document.getElementById("tour-card-body");
const tourSkipBtn = document.getElementById("tour-skip-btn");
const tourSpeakBtn = document.getElementById("tour-speak-btn");
const tourBackBtn = document.getElementById("tour-back-btn");
const tourNextBtn = document.getElementById("tour-next-btn");
const tourBtn = document.getElementById("tour-btn");

const TOUR_SEEN_KEY = "ai_pear_review_tour_seen";
const TOUR_SPEECH_ON_KEY = "ai_pear_review_tour_speech_on";
const TOUR_REDUCED_MOTION = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

// The app's own configured TTS service (voice_service.py), via
// "speak_text" -> "tour_audio_chunk" (see try_speak's docstring in
// web/speech.py) — not window.speechSynthesis. Reusing the real service means
// tour audio sounds like the rest of the app rather than switching to
// whatever voice the OS happens to ship, and it's the service a reviewer
// setting up self-hosted STT/TTS for the first time would actually be
// checking works.
//
// That also means tour audio depends on a service that's genuinely
// optional and often not configured yet — exactly when the tour matters
// most. state.ttsAvailable (updateStatus, above) already tracks this live from
// service_status, the same signal the header's TTS pill reads, so this
// doesn't invent a second "is it working" check. The button itself is
// never hidden the way an unsupported browser API would be: reachability
// can change at runtime (the service starting or stopping), so a
// permanent hide based on a one-time check would go stale.
let tourSpeechOn = false;
try {
  tourSpeechOn = localStorage.getItem(TOUR_SPEECH_ON_KEY) === "1";
} catch {
  // non-critical, ignore
}
tourSpeakBtn.setAttribute("aria-pressed", String(tourSpeechOn));

// Called from updateStatus (above) whenever service_status reports TTS
// down — keeps the speak button's aria-pressed/localStorage state honest
// with what will actually happen next, rather than leaving it showing
// "on" for a service that just failed. Idempotent: a no-op if the toggle
// was already off, so calling it on every "still down" update (not just
// the transition into it) is harmless.
export function onTourTtsUnavailable() {
  if (!tourSpeechOn) return;
  tourSpeechOn = false;
  tourSpeakBtn.setAttribute("aria-pressed", "false");
  try {
    localStorage.setItem(TOUR_SPEECH_ON_KEY, "0");
  } catch {
    // non-critical, ignore
  }
  send("stop");
  stopTourAudioQueue();
}

function speakTourStep(step) {
  if (!tourSpeechOn || !state.ttsAvailable) return;
  // send()'s own speak_text branch stops whatever else is playing first
  // (narration, a file read, or the previous step's own tour audio) —
  // callers don't need to do that themselves, same as speak_file's own
  // callers don't.
  //
  // A period between title and body, even though the body's own text
  // already ends in punctuation — without it the TTS service tends to run
  // the two straight together with no pause, since there's no sentence
  // boundary between them in the DOM's separate elements, only in how
  // they're laid out visually.
  send("speak_text", { text: `${step.title}. ${step.body}` });
}

tourSpeakBtn.addEventListener("click", () => {
  if (!state.ttsAvailable) {
    // "if needs be, pop up the settings" — rather than silently doing
    // nothing (which is exactly the failure mode this whole feature is
    // built to avoid elsewhere), take the reviewer straight to where the
    // TTS endpoint is configured instead of turning speech on for a
    // service that isn't there. Does NOT flip tourSpeechOn — nothing
    // was actually enabled, so the button staying unpressed is accurate.
    openModelSettingsPanel();
    return;
  }
  tourSpeechOn = !tourSpeechOn;
  tourSpeakBtn.setAttribute("aria-pressed", String(tourSpeechOn));
  try {
    localStorage.setItem(TOUR_SPEECH_ON_KEY, tourSpeechOn ? "1" : "0");
  } catch {
    // non-critical, ignore
  }
  if (tourSpeechOn && tourStepIndex !== -1) {
    speakTourStep(TOUR_STEPS[tourStepIndex]);
  } else {
    // Turning off mid-playback: stop immediately rather than waiting for
    // an in-flight chunk to finish. Mirrors the Interrupt button's own
    // belt-and-braces pattern (cancel server-side AND hard-stop whatever
    // audio is already buffered client-side) rather than relying solely
    // on send()'s conditional stopAllAudio, which only fires for message
    // types that are actually sent — nothing is sent in this branch.
    send("stop");
    stopTourAudioQueue();
  }
});

// selector: null for the two centered, targetless steps (intro/outro).
// onEnter/onExit: only the file-explorer step needs these — it starts
// collapsed, so the step opens it and puts it back to whatever it was
// before, rather than leaving the reviewer's own collapsed/expanded
// preference changed just because they looked at the tour once.
const TOUR_STEPS = [
  {
    selector: null,
    title: "A quick tour",
    body: "This walks through the main pieces of the review UI — a couple of minutes, and you can leave any time with Skip or Escape.",
  },
  {
    selector: "#status-bar",
    title: "Service status",
    body: "This bar shows whether the app can reach each service it depends on — ok, or down. LLM (large language model) is the AI model that narrates and answers you. Briefing adds extra background for each change. STT (speech-to-text) turns your voice into text, and TTS (text-to-speech) reads replies aloud. Only LLM is essential: if nothing narrates, check it first — usually Ollama isn't running, or the model isn't downloaded. LLM is checked as the page loads; the others show ok until first used, then report what happened. Tokens counts how much text has gone to and from the model.",
  },
  {
    selector: "#start-review-btn",
    title: "Start Review",
    body: "Browsing the diff always works, but chat and explanations stay off until you click this. Nothing is sent anywhere before you do.",
  },
  {
    selector: ".toolbar .controls",
    title: "Moving through the diff",
    body: "Prev / Next step through one hunk — one block of changed lines — at a time. Once the review has started, Mark as reviewed, Review all and End Review appear here too — reviewing every hunk, or ending early, takes you to a summary. The Explain button asks the AI to explain the change you're on — nothing is explained until you click it. To have every change explained as you move to it instead, set Explain changes to Automatically in the chat's settings.",
  },
  {
    // #file-list, not #file-sidebar-tab (the small expand/collapse arrow)
    // — the step talks about the file rows and the folder-browse icon
    // inside the panel, so the spotlight should cover the panel those
    // actually live in, not the toggle beside it.
    selector: "#file-list",
    title: "File explorer",
    body: "Every changed file — click one to jump to its first hunk. The folder icon switches to browsing the whole repo, not just what changed.",
    onEnter() {
      this._wasOpen = !fileSidebarEl.classList.contains("collapsed");
      if (!this._wasOpen) setFileSidebarOpen(true);
    },
    onExit() {
      if (!this._wasOpen) setFileSidebarOpen(false);
    },
  },
  {
    selector: "#view-mode-toggle",
    title: "Merged / Split view",
    body: "Switch how the diff renders. Hidden automatically when there's nothing to diff — an unchanged file. The refresh icon beside it re-reads the diff after you've changed files outside the app.",
  },
  {
    selector: "#code-view",
    title: "Marking lines and comments",
    body: "Double-click a line (or use the dot in the gutter) to mark it as context for your next message. The + button on a line opens an inline review comment, GitHub-PR-review style — queued comments become a plan for your coding agent when you click Create plan.",
  },
  {
    selector: "#text-input",
    title: "Chat",
    body: "Ask questions, discuss alternatives, or request a change — by typing or with the mic button. Overall/File tabs above switch between every turn and just this file's. When an answer depends on code the narrator can't see, the Look deeper button under it hands the question to your coding agent to investigate, read-only.",
  },
  {
    selector: "#act-now-btn",
    title: "Act Now",
    body: "Toggle this on, then describe a small change in chat. Your coding agent makes it in a copy of the repo, and you get a preview to confirm before anything is written to disk. Needs a coding agent chosen in settings first — until then it's greyed out.",
  },
  {
    selector: "#review-queue-row",
    title: "Create plan",
    body: "Once you've queued a comment or two, this turns them into a plan for your coding agent — saved as a file, and optionally as an /apply-review skill for Claude Code or Cline. It opens in the preview, and View review plan brings it back later.",
  },
  {
    selector: "#tour-btn",
    title: "Settings, and the end",
    body: "This ? button replays the guided tour any time. For settings, open the gear under the chat's message box: the model and provider, voice endpoints, and the coding agent Act Now and Look deeper use — plus whether the optional prep skills (run in Claude Code or Cline) are up to date.",
  },
];

let tourStepIndex = -1;
let tourReturnFocusEl = null;
let tourStepCleanup = null; // this step's own onExit, bound with the right `this`

function isTourTargetVisible(el) {
  return !!el && el.getClientRects().length > 0;
}

function tourStepTarget(step) {
  return step.selector ? document.querySelector(step.selector) : null;
}

// The indices of every step whose target exists right now (the two
// targetless steps, selector: null, always count). Recomputed on every
// navigation rather than cached at tour-start, since which controls are
// on screen can genuinely change mid-tour — Start Review disappearing the
// moment it's clicked, most obviously.
//
// This is also what "Step N of TOTAL" counts against, not TOUR_STEPS'
// raw length — an earlier version used the raw array index, which
// produced a progress readout that could jump "Step 1... Step 3..." the
// moment a step in between got skipped, and made the last-step ("Done")
// check fire too late or too early depending on how many steps before it
// happened to be hidden. Counting the actually-visible sequence fixes
// both at once.
export function tourVisibleStepIndices() {
  return TOUR_STEPS.map((_, i) => i).filter((i) => {
    const step = TOUR_STEPS[i];
    return !step.selector || isTourTargetVisible(tourStepTarget(step));
  });
}

function positionTourHighlight(target) {
  if (!target) {
    tourHighlightEl.classList.add("tour-no-target");
    Object.assign(tourHighlightEl.style, { top: "0px", left: "0px", width: "0px", height: "0px" });
    return;
  }
  tourHighlightEl.classList.remove("tour-no-target");
  // 6px of breathing room around the real element, so the highlight reads
  // as "this thing" rather than clipping tight to its border.
  const pad = 6;
  const rect = target.getBoundingClientRect();
  Object.assign(tourHighlightEl.style, {
    top: `${Math.max(0, rect.top - pad)}px`,
    left: `${Math.max(0, rect.left - pad)}px`,
    width: `${rect.width + pad * 2}px`,
    height: `${rect.height + pad * 2}px`,
  });
}

function positionTourCard(target) {
  // Measure after layout has settled from positionTourHighlight's own
  // style writes, so the card's own size (which can vary with its text)
  // is read correctly rather than from a stale previous step.
  const cardRect = tourCardEl.getBoundingClientRect();
  const margin = 12;

  if (!target) {
    tourCardEl.style.top = `${(window.innerHeight - cardRect.height) / 2}px`;
    tourCardEl.style.left = `${(window.innerWidth - cardRect.width) / 2}px`;
    return;
  }

  const rect = target.getBoundingClientRect();
  // Tried in this order and the first one that actually fits without
  // clamping wins — below is usually right, but a target that's tall and
  // narrow (the file explorer, spanning nearly the full viewport height)
  // leaves no real room above or below it, and centering the card
  // horizontally on a narrow target then plants it right on top of the
  // very thing being pointed at. Found by looking at a screenshot, not by
  // reasoning about the math: below/above only, unconditionally, put the
  // card over the file list's own header on that step.
  const candidates = [
    { top: rect.bottom + margin, left: rect.left + rect.width / 2 - cardRect.width / 2 }, // below
    { top: rect.top - cardRect.height - margin, left: rect.left + rect.width / 2 - cardRect.width / 2 }, // above
    { top: rect.top + rect.height / 2 - cardRect.height / 2, left: rect.right + margin }, // right
    { top: rect.top + rect.height / 2 - cardRect.height / 2, left: rect.left - cardRect.width - margin }, // left
  ];
  const fits = (c) =>
    c.top >= margin && c.top + cardRect.height <= window.innerHeight - margin &&
    c.left >= margin && c.left + cardRect.width <= window.innerWidth - margin;

  const chosen = candidates.find(fits) || candidates[0];
  // Clamp fully on-screen regardless — even the chosen candidate (or the
  // below-target fallback, if literally nothing fit) must never place the
  // card partly off whatever viewport size is actually available.
  const top = Math.min(Math.max(margin, chosen.top), window.innerHeight - cardRect.height - margin);
  const left = Math.min(Math.max(margin, chosen.left), window.innerWidth - cardRect.width - margin);
  tourCardEl.style.top = `${top}px`;
  tourCardEl.style.left = `${left}px`;
}

function renderTourStep(index) {
  if (tourStepCleanup) {
    try {
      tourStepCleanup();
    } catch {
      // a step's own cleanup failing must never trap the tour open
    }
    tourStepCleanup = null;
  }

  tourStepIndex = index;
  const step = TOUR_STEPS[index];
  const target = tourStepTarget(step);

  if (step.onEnter) step.onEnter();
  if (step.onExit) tourStepCleanup = () => step.onExit();

  if (target) {
    target.scrollIntoView({ behavior: TOUR_REDUCED_MOTION ? "auto" : "smooth", block: "center" });
  }

  const visible = tourVisibleStepIndices();
  const position = visible.indexOf(index); // this step's place in the VISIBLE sequence, not its raw array index

  tourCardTitleEl.textContent = step.title;
  tourCardBodyEl.textContent = step.body;
  tourCardProgressEl.textContent = `Step ${position + 1} of ${visible.length}`;
  speakTourStep(step);
  tourBackBtn.disabled = position <= 0;
  // innerHTML here is a fixed literal, same trusted-literal-only contract
  // every other iconHtml()/hardcoded-markup call site in this file already
  // follows — never anything derived from server/user data.
  tourNextBtn.innerHTML =
    position === visible.length - 1 ? "Done" : 'Next <svg class="icon"><use href="#icon-chevron-right"></use></svg>';

  // A smooth scrollIntoView takes a moment to settle; positioning on the
  // next frame (rather than immediately) reads the rect after that
  // animation has actually moved the element, not before it started.
  requestAnimationFrame(() => {
    positionTourHighlight(target);
    positionTourCard(target);
  });

  tourNextBtn.focus();
}

function startTour() {
  tourReturnFocusEl = document.activeElement;
  tourHighlightEl.classList.remove("hidden");
  tourCardEl.classList.remove("hidden");
  document.addEventListener("keydown", onTourKeydown);
  window.addEventListener("resize", onTourReposition);
  window.addEventListener("scroll", onTourReposition, true);
  renderTourStep(0);
  try {
    localStorage.setItem(TOUR_SEEN_KEY, "1");
  } catch {
    // non-critical, ignore
  }
}

function endTour() {
  if (tourStepIndex === -1) return; // not open — guards a double Escape/click
  send("stop");
  stopTourAudioQueue();
  if (tourStepCleanup) {
    try {
      tourStepCleanup();
    } catch {
      // as above
    }
    tourStepCleanup = null;
  }
  tourStepIndex = -1;
  tourHighlightEl.classList.add("hidden");
  tourCardEl.classList.add("hidden");
  document.removeEventListener("keydown", onTourKeydown);
  window.removeEventListener("resize", onTourReposition);
  window.removeEventListener("scroll", onTourReposition, true);
  // Return focus to whatever opened the tour (the ? button, or wherever
  // focus was for an auto-started tour) rather than leaving it stranded on
  // a now-hidden Next button.
  if (tourReturnFocusEl && document.contains(tourReturnFocusEl)) tourReturnFocusEl.focus();
  tourReturnFocusEl = null;
}

function tourNext() {
  const visible = tourVisibleStepIndices();
  const position = visible.indexOf(tourStepIndex);
  if (position === -1 || position === visible.length - 1) endTour();
  else renderTourStep(visible[position + 1]);
}

function tourBack() {
  const visible = tourVisibleStepIndices();
  const position = visible.indexOf(tourStepIndex);
  if (position > 0) renderTourStep(visible[position - 1]);
}

function onTourReposition() {
  if (tourStepIndex === -1) return;
  const target = tourStepTarget(TOUR_STEPS[tourStepIndex]);
  positionTourHighlight(target);
  positionTourCard(target);
}

function onTourKeydown(e) {
  if (e.key === "Escape") { e.preventDefault(); endTour(); }
  else if (e.key === "ArrowRight" || e.key === "Enter") { e.preventDefault(); tourNext(); }
  else if (e.key === "ArrowLeft") { e.preventDefault(); tourBack(); }
}

tourBtn.addEventListener("click", () => (tourStepIndex === -1 ? startTour() : endTour()));
tourSkipBtn.addEventListener("click", endTour);
tourNextBtn.addEventListener("click", tourNext);
tourBackBtn.addEventListener("click", tourBack);

// Auto-start: once per browser, ever, and only when there's actually a
// diff to look at — review_progress's `total` is the earliest signal for
// that. Guarded on tourStepIndex too so a reconnect mid-tour (or a second
// review_progress on the same connection) can't restart it underneath the
// reviewer. Not gated on narration_available: the tour is about the UI,
// which is worth seeing even in degraded mode.
let tourAutoStartChecked = false;
export function maybeAutoStartTour(total) {
  if (tourAutoStartChecked || tourStepIndex !== -1 || !(total > 0)) return;
  tourAutoStartChecked = true;
  let seen = null;
  try {
    seen = localStorage.getItem(TOUR_SEEN_KEY);
  } catch {
    // If storage is blocked, err toward showing it once per page load
    // rather than nagging on every single load — same tradeoff every
    // other localStorage-backed preference here already makes silently.
  }
  if (seen) return;
  // A beat after the page's own state (status bar, file list) has settled,
  // so the tour doesn't compete with everything else appearing at once.
  setTimeout(startTour, 600);
}
