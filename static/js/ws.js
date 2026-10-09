import {
  enqueueFileAudioChunk,
  enqueueTourAudioChunk,
  playAudio,
  playTurnAudio,
  stopAudioForSend,
} from "./audio.js";
import {
  clearThinking,
  failDeeperIfPending,
  onDeeperTurn,
  onHumanTurn,
  onNarration,
  onReviewerTurn,
} from "./transcript.js";
import {
  showError,
  showNotice,
  updateReviewProgress,
  updateStatus,
} from "./status.js";
import { onAllFiles } from "./sidebar.js";
import { onContextTooLarge, onSettings } from "./settings.js";
import { onDefinition, onFileExplore, onPresenting } from "./review-flow.js";
import { sendVoicePrefs } from "./prefs.js";
import { onMdPreview } from "./md-preview.js";
import { clearMarkedLines } from "./interactions.js";
import {
  audioEl,
  backToSummaryBtn,
  reopenReviewBtn,
  errorBannerEl,
  interruptBtn,
  nextBtn,
  prevBtn,
  refreshDiffBtn,
  reviewAllToggleEl,
  reviewedBtn,
} from "./dom.js";
import {
  onActNowCleared,
  onActNowPreview,
  onActNowStopped,
  onReviewCommentQueued,
  onReviewCommentRemoved,
  onReviewCommentUpdated,
  onReviewCommentsSync,
  onReviewFinished,
} from "./comments.js";
// The WebSocket and the one place every outbound message goes.
//
// The dispatch below is a flat switch on msg.type: one case per message,
// each handing straight to the module that owns it. Nothing else in the
// app touches the socket — send() is the single choke point, which is why
// the audio-stopping rule and the test seam both hang off it.

// The "Explain changes" preference (prefs.js) rides on the URL rather than
// in a message: the server presents the first hunk as soon as the socket
// opens, before it could read one, and must already know not to explain it.
export const AUTO_NARRATE_KEY = "ai_pear_review_auto_narrate";

function connectionQuery() {
  try {
    // On request unless explicitly set to automatic — matching prefs.js.
    return localStorage.getItem(AUTO_NARRATE_KEY) === "1" ? "" : "?auto_narrate=0";
  } catch {
    return "?auto_narrate=0";
  }
}

const ws = new WebSocket(`ws://${location.host}/ws${connectionQuery()}`);
ws.addEventListener("open", sendVoicePrefs);

ws.addEventListener("message", (event) => {
  const msg = JSON.parse(event.data);
  switch (msg.type) {
    case "presenting":
      onPresenting(msg.payload);
      break;
    case "human_turn":
      onHumanTurn(msg.payload);
      break;
    case "reviewer_turn":
      onReviewerTurn(msg.payload);
      break;
    case "deeper_turn":
      onDeeperTurn(msg.payload);
      break;
    case "audio_chunk":
      playAudio(msg.payload);
      break;
    case "error":
      failDeeperIfPending(msg.payload.message);
      showError(msg.payload.message);
      break;
    case "service_status":
      updateStatus(msg.payload);
      break;
    case "review_progress":
      updateReviewProgress(msg.payload);
      break;
    case "narration":
      onNarration(msg.payload);
      break;
    case "definition":
      onDefinition(msg.payload);
      break;
    case "notice":
      showNotice(msg.payload.message, msg.payload.level);
      break;
    case "prep_status":
      // Briefings behind the code (handlers/prep.py): said when the review
      // opens or the diff is refreshed. The message says how to brief them.
      if (msg.payload.message) showNotice(msg.payload.message, "info");
      break;
    case "settings":
      onSettings(msg.payload);
      break;
    case "context_too_large":
      onContextTooLarge(msg.payload);
      break;
    case "review_comments_sync":
      onReviewCommentsSync(msg.payload);
      break;
    case "review_comment_queued":
      onReviewCommentQueued(msg.payload);
      break;
    case "review_comment_updated":
      onReviewCommentUpdated(msg.payload);
      break;
    case "review_comment_removed":
      onReviewCommentRemoved(msg.payload);
      break;
    case "review_finished":
      onReviewFinished(msg.payload);
      break;
    case "act_now_preview":
      onActNowPreview(msg.payload);
      break;
    case "act_now_cleared":
      onActNowCleared(msg.payload);
      break;
    case "agent_stopped":
      if (msg.payload.kind === "look_deeper") failDeeperIfPending(msg.payload.message, "Look deeper was stopped");
      else onActNowStopped(msg.payload);
      break;
    case "file_audio_chunk":
      enqueueFileAudioChunk(msg.payload);
      break;
    case "turn_audio_chunk":
      playTurnAudio(msg.payload);
      break;
    case "tour_audio_chunk":
      enqueueTourAudioChunk(msg.payload);
      break;
    case "md_preview":
      onMdPreview(msg.payload);
      break;
    case "all_files":
      onAllFiles(msg.payload);
      break;
    case "file_explore":
      onFileExplore(msg.payload);
      break;
    default:
      console.warn("unknown message type", msg);
  }
});

ws.addEventListener("close", () => {
  // Don't clobber a more specific error (e.g. "no changes found") that the
  // server sent right before closing — only show the generic message if
  // nothing more specific is already displayed.
  if (errorBannerEl.classList.contains("hidden")) {
    showError("Connection to server lost. Reload to reconnect.");
  }
});

export function send(type, payload = {}) {
  // Test seam (see window.__app at the end of this file). qa_agent drives
  // the UI like a user for everything it can, but the tour's speak path
  // has no such route: asserting what a click requested would otherwise
  // mean standing up a real TTS service. A hook here rather than a
  // replaceable global because module scope makes app internals
  // unreachable from page.evaluate. Null in production.
  if (window.__app && window.__app.sendHook) {
    window.__app.sendHook(type, payload);
    return;
  }
  stopAudioForSend(type);
  ws.send(JSON.stringify({ type, payload }));
}

// Disabled immediately on click, re-enabled by reenableActionButtons()
// once the expected response (or an error) arrives — prevents a rapid
// double-click from queuing two hunk jumps before the first one lands.
// Next/Prev disable each other too since both mutate the same server-side
// session.index in sequence.
nextBtn.addEventListener("click", () => {
  nextBtn.disabled = true;
  prevBtn.disabled = true;
  send("next");
});
prevBtn.addEventListener("click", () => {
  nextBtn.disabled = true;
  prevBtn.disabled = true;
  send("prev");
});
interruptBtn.addEventListener("click", () => {
  send("stop"); // cancels in-flight LLM/TTS generation server-side
  audioEl.pause(); // ...but that does nothing for audio already delivered and playing
  audioEl.currentTime = 0;
  // Cancelling server-side means whatever message would normally have
  // cleared this (a reply/narration/error) now never arrives — nothing
  // else will remove the "…thinking" bubble if we don't do it here.
  clearThinking();
});
reviewedBtn.addEventListener("click", () => send("toggle_reviewed"));
backToSummaryBtn.addEventListener("click", () => send("show_summary", {}));
reopenReviewBtn.addEventListener("click", () => send("reopen_review", {}));
refreshDiffBtn.addEventListener("click", () => {
  refreshDiffBtn.disabled = true; // re-enabled by reenableActionButtons() once the resulting "presenting" arrives
  // Marked lines hold a reference into a specific full_lines array (see
  // state.markedLines' own docstring above) that stays valid across ordinary
  // hunk navigation but not across a refresh — the server rebuilds the
  // diff from scratch, so old line-number references can point at
  // content that's shifted or gone. Without this the chip and highlight
  // silently go stale and the next reply would carry pre-refresh code as
  // hidden context.
  clearMarkedLines();
  send("refresh_diff");
});
reviewAllToggleEl.addEventListener("click", () => send("toggle_reviewed_all"));

// Active -> the next text/voice message is applied immediately (after a
// preview/confirm step) rather than a plain reply — see sendTextReply/
// onBottomMicDone, one-shot, auto-deactivates after that one message.
// Visual cue on the input itself so the mode isn't just an easy-to-forget
// button state. See state.actNowActive/actNowStatus for what backs it.
