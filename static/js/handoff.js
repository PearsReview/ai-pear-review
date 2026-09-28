// The Create plan dialog: an optional overall note, and whether to hand the
// plan off as a file only or also as the apply-review agent skill (see
// handle_finish_review in app/handlers/comments.py).
//
// Opened two ways. From Create plan it offers Create plan / Cancel. From End
// Review while comments are still queued it offers Create plan & end / End
// without plan / Cancel, so the queue isn't left behind by accident.
//
// The file-or-skill choice is remembered per browser in localStorage — a
// per-viewer convenience, so every read and write is guarded: a blocked or
// empty store just means the default, "Plan file only".

import { send } from "./ws.js";
import { state } from "./state.js";
import {
  endReviewBtn,
  finishReviewBtn,
  handoffCancelBtn,
  handoffCreateBtn,
  handoffDialog,
  handoffEndOnlyBtn,
  handoffIntro,
  handoffNote,
} from "./dom.js";

const AS_SKILL_KEY = "handoffAsSkill";

let fromEndReview = false;

function savedAsSkill() {
  try {
    return localStorage.getItem(AS_SKILL_KEY) === "1";
  } catch {
    return false;
  }
}

function saveAsSkill(asSkill) {
  try {
    localStorage.setItem(AS_SKILL_KEY, asSkill ? "1" : "0");
  } catch {
    // Not remembered this time; the choice itself still goes through.
  }
}

function chosenAsSkill() {
  const checked = handoffDialog.querySelector('input[name="handoff-as"]:checked');
  return !!checked && checked.value === "skill";
}

export function openHandoffDialog({ fromEndReview: fromEnd = false } = {}) {
  fromEndReview = fromEnd;
  const count = state.queuedComments.length;
  const comments = `${count} queued comment${count === 1 ? "" : "s"}`;
  handoffIntro.textContent = fromEnd
    ? `You have ${comments}. Create the plan from ${count === 1 ? "it" : "them"} before ending the review?`
    : `Turns your ${comments} into a plan for your coding agent, and clears the queue.`;
  handoffCreateBtn.textContent = fromEnd ? "Create plan & end" : "Create plan";
  handoffEndOnlyBtn.classList.toggle("hidden", !fromEnd);
  handoffNote.value = "";
  const asSkill = savedAsSkill();
  handoffDialog.querySelector(`input[name="handoff-as"][value="${asSkill ? "skill" : "file"}"]`).checked = true;
  handoffDialog.showModal();
  handoffCreateBtn.focus();
}

function endReview() {
  endReviewBtn.classList.add("hidden"); // same as End Review's own click: it never comes back
  send("end_review", {});
}

handoffCreateBtn.addEventListener("click", () => {
  const asSkill = chosenAsSkill();
  saveAsSkill(asSkill);
  handoffDialog.close();
  finishReviewBtn.disabled = true; // re-enabled by onReviewFinished (or showError) via updateReviewQueueUI
  send("finish_review", { note: handoffNote.value.trim(), as_skill: asSkill });
  // Handled in order server-side (both are inline handlers), so the plan is
  // written before the review ends.
  if (fromEndReview) endReview();
});

handoffEndOnlyBtn.addEventListener("click", () => {
  handoffDialog.close();
  endReview();
});

handoffCancelBtn.addEventListener("click", () => handoffDialog.close());
