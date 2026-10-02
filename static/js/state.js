// Shared UI state — the fields more than one module both reads and writes.
//
// Data only: this module imports nothing and contains no functions. The
// setters that pair a field with a DOM update (setSpeakingFile,
// setCodeViewMode, setActNowActive, setFollowReading, setDiffViewMode) stay
// in the module that owns that behaviour and remain the only writers of
// their field.
//
// Properties on one object rather than separate exports because an imported
// binding is read-only for the importer: `import { markedLines }` followed
// by `markedLines = []` is a TypeError. A property has no such restriction,
// so every field below can be assigned from wherever it belongs.
//
// Anything a single module owns (narrationQueue, tourStepIndex,
// collapsedFolders, allFiles, expandedCommentIds, mediaRecorder, ...) stays
// a local in that module. This is a shared store, not a home for everything
// that happens to be mutable.

export const state = {
  // --- Service reachability (from service_status) -------------------------
  sttAvailable: true,
  ttsAvailable: true,
  llmAvailable: true,
  briefingAvailable: true,

  // Reviewer-controlled preference, distinct from the reachability flags
  // above — this is "the reviewer doesn't want voice right now", not "the
  // voice service is down". Gates both the mic button and audio playback
  // client-side, and is mirrored to the server (see sendVoicePrefs) so it
  // can skip the real TTS HTTP call entirely rather than just having the
  // browser stay silent after paying for synthesis.
  sttPrefOn: true,
  ttsPrefOn: true,
  // Whether moving to an unexplained hunk explains it automatically (the
  // "Explain changes" preference, prefs.js). Off by default: the toolbar's
  // Explain button does it on request (explain.js), so no model call is
  // spent on a hunk the reviewer only skims past.
  autoNarrate: false,

  // --- Which file/hunk is on screen ---------------------------------------
  currentFilePath: "", // drives the active-pill highlight in the file list

  // The hunk on screen, as the Explain button (explain.js) needs it: its
  // index, whether it has been explained, and whether it can be (a real
  // hunk, review running, a model to ask). Set by onPresenting/onNarration.
  currentHunkIndex: -1,
  hunkExplained: false,
  hunkExplainable: false,
  // True while the chat's "…thinking" placeholder shows — a response is
  // already on its way (see showThinking/clearThinking).
  thinking: false,

  // Most recent presenter bubble, so the replay button can be attached when
  // that turn's audio arrives. Written by the transcript, read by the audio
  // queue — two modules, so it cannot be a local in either.
  lastPresenterTurnEl: null,

  // The file currently open via "explore_file" (a sidebar click on a no-hunk
  // pill), or null while looking at a real hunk/summary — drives which wire
  // message the reply composer sends (see sendTextReply/onBottomMicDone) and
  // is cleared again in backToHunk. Distinct from currentFilePath (which
  // "File chat" filtering reads and this also updates) since an explored
  // file was never part of session.hunks at all.
  exploringFilePath: null,

  // Mirrors the last "presenting" payload's review_started, so backToHunk can
  // correctly restore the comments-locked gate (marking/queuing, not the
  // reply composer — see setComposerEnabled) after leaving explore mode
  // (where commenting has no hunk to anchor to at all — see onFileExplore).
  lastKnownReviewStarted: false,

  // Tracks review_ended independently of "is the summary screen currently
  // showing" — a real hunk can be on screen while this is true (browsing
  // after the review has ended; see present_current_hunk's docstring in
  // handlers/narration.py), and several things need to stay locked in exactly
  // that state: Mark-as-reviewed/Review-all, the composer (replies/Act Now),
  // and inline comments. Updated from both "presenting" and "review_progress",
  // since either can arrive first after a transition.
  reviewEnded: false,

  // How many hunks this session has at all. Mirrored from both "presenting"
  // and "review_progress" for the same reason reviewEnded above is: either
  // can arrive first after a transition. Zero is its own state, not just a
  // small number — a repo with nothing to review has nowhere for Prev/Next
  // to go and nothing for "Back to hunk" to go back to, so both are turned
  // off rather than left live and dead (see updateHunkNavAvailability in
  // status.js and showBackToHunkBtn in review-flow.js).
  totalHunks: 0,

  // The reviewer's current text selection in the code view. Written by the
  // code-view mouseup handler, read by Step Into and by the button
  // re-enabling logic — two modules, so it cannot be a local.
  currentSelection: "",

  // --- What the code view is showing --------------------------------------
  // Cached so "Step Into" can restore the current hunk's view on "Back"
  // without a server round-trip — the full diff content is already sitting in
  // the browser from the last "presenting" message.
  savedHunkView: null, // { hunkMetaText, fullLines, highlightStart, highlightEnd }

  // The exact args of the last renderCodeView() call — separate from
  // savedHunkView above, which is specifically for the "Step Into" -> "Back"
  // round trip. This is what the Merged/Split toggle re-renders from, so
  // switching modes doesn't need a server round-trip and works for a
  // "Step Into" definition peek too, not just the current hunk.
  lastRenderedView: null, // { fullLines, highlightStart, highlightEnd, filePath }

  diffViewMode: "merged", // "merged" | "split"

  // Which renderer currently owns #code-view. The diff renderers and the
  // markdown preview can't share renderCodeView (a preview isn't full_lines-
  // shaped), so anything that re-enters a render needs to know which one is on
  // screen — otherwise rerenderCurrentView()'s call sites would happily redraw
  // a stale hunk straight over the preview. Set inside renderCodeView and
  // showEmptyCodeView, the only other two functions that take over #code-view,
  // so every existing diff path resets it for free.
  codeViewMode: "diff", // "diff" | "md-preview"

  // --- Markdown preview ----------------------------------------------------
  mdPreview: null, // { filePath, contentHash, blocks: [...] }

  // Selected block indices, capped at 2 and expanded to everything between
  // them — deliberately NOT markedLines, which buildMarkedContext() turns into
  // hidden LLM prompt context on every reply/request_change. A preview
  // selection is a "read this part aloud" instruction and must never leak into
  // a prompt.
  mdSelectedBlocks: [],
  mdReadingChunk: null, // the file_audio_chunk currently *playing*, or null
  mdReadingBlockIndex: null, // which block inside it the audio is estimated to be on
  mdFollowReading: true, // auto-scroll to the block being read, until the reviewer scrolls themselves

  // file_path currently being read aloud via speak_file, or null — drives
  // which .md pill shows a stop affordance instead of the speaker icon
  speakingFilePath: null,

  // --- Line marking --------------------------------------------------------
  // Eclipse-style "breakpoint" line markers (double-click a line to toggle).
  // Capped at 2: 1 marks a single line for context, 2 (same file) expands to
  // every line *between* them inclusive when actually sent (see
  // buildMarkedContext) — marking a 3rd, different line starts over rather
  // than trying to guess what 3+ marks should mean. Each entry keeps a
  // reference to the fullLines array it was marked from, so the "between"
  // range can be resolved later even if the reviewer has since navigated to a
  // different hunk (full_lines is the *whole file*, shared across all of that
  // file's hunks — see diff_service.py — so this reference stays valid as long
  // as the file hasn't been refreshed).
  markedLines: [], // [{ filePath, oldLineno, newLineno, fullLines }]

  // --- Inline review comments ----------------------------------------------
  // Inline GitHub-PR-style review comments. Both composingComment and
  // queuedComments are read at render time by renderMergedCodeView/
  // renderSplitCodeView (see buildLineCommentRowHtml) — same stateless
  // full-rebuild model as markedLines/highlight above, no DOM diffing.
  //
  // composingComment is ephemeral — it exists only between clicking "+" and
  // Send/Cancel/navigating away:
  // { anchorLines: [{file_path, old_lineno, new_lineno, text}, ...]
  //   (buildMarkedContext()-shaped), filePath, lastOldLineno, lastNewLineno,
  //   text, severity }
  composingComment: null,

  // Client-side mirror of session.pending_review_comments, kept in sync via
  // review_comments_sync (on connect) and review_comment_queued/_updated/
  // _removed/review_finished: [{id, file_path, where, instruction, severity,
  // anchor}]. anchor is {first_old_lineno, first_new_lineno, last_old_lineno,
  // last_new_lineno} | null (see server.py's _line_context) — badges/cards
  // anchor on last_*_lineno, matching GitHub's own convention of showing a
  // comment after the last line of whatever range it's attached to.
  queuedComments: [],

  // Which queued-comment cards are open, and which one is showing its edit
  // textarea. Both the comment handlers and the code-view click delegation
  // write these, so they cannot be module-local: an imported binding is
  // read-only, and `editingCommentId = x` from another module throws.
  expandedCommentIds: new Set(), // ids showing their full card, not just the badge
  editingCommentId: null, // id currently showing its edit textarea, or null

  // --- Act Now -------------------------------------------------------------
  // Act Now is one-shot by design — the mode clears the moment a request is
  // sent (see sendTextReply) — but that used to be permanent even when the
  // request then failed server-side. This tracks "the request currently in
  // flight was an act_now the reviewer hasn't seen the outcome of yet", so
  // showError can re-arm it on failure and a retry goes out as another Act Now
  // instead of silently becoming a plain chat reply.
  actNowPending: false,

  // A "refine_act_now" is in flight: the preview bar is locked, and an error
  // puts it back as it was (the previous preview is still the pending one)
  // rather than hiding it — see endActNowRefine.
  actNowRefining: false,

  // Repo-relative path of the newest review plan (.review/review_*.md), for
  // the "View review plan" buttons — null until one exists.
  lastReviewPlan: null,
  // {message, level}: a notice to show once the next markdown preview has
  // opened — see onReviewFinished, whose own notice that preview would
  // otherwise clear.
  noticeAfterPreview: null,
  // {planFile, instruction} for the plan just created, so the preview's Copy
  // instruction button copies what the server said to give the agent
  // (/apply-review when the skill was saved).
  handoff: null,

  // The one source of truth for "is Act Now on" — every read goes through
  // this, never the DOM. (A real button, not a checkbox. "Request Change"
  // used to live here too, mutually exclusive with Act Now; it's now the
  // inline per-line comment composer in the code view instead, see
  // openComposerForLine.)
  actNowActive: false,

  // service_status's "act_now": whether a coding agent is configured and
  // installed (see harness_status server-side). Off until the server says so,
  // so the button never offers something that would only answer with an error.
  actNowStatus: { available: false, detail: "Checking for a coding agent…", agent: "" },
};
