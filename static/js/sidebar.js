// The file sidebar: the filterable file list, its folder tree, and the
// edge-tab collapse behaviour shared with the chat pane.
//
// Owns the per-pill speak and preview buttons too. They decorate a .md
// file's row, so they live with the code that builds the row rather than
// with the audio and preview modules they talk to — which is also what
// keeps the dependency one-way instead of sidebar and audio importing each
// other.
//
// The file list is re-rendered from a cached copy (allFiles) on every
// review_progress message and on every filter keystroke, so counts and the
// active-file highlight stay current without a server round-trip.

import { state } from "./state.js";
import { send } from "./ws.js";
import {
  exploreAllFilesBtn,
  fileFilterInput,
  fileListItemsEl,
  fileSidebarEl,
  fileSidebarTab,
  fileSidebarTabIcon,
  filesMenuLabel,
  fileListEl,
  makeIconSpan,
  chatPaneEl,
  chatPaneContentEl,
  chatPaneTab,
  chatPaneTabIcon,
  iconHtml,
} from "./dom.js";
import { setSpeakingFile } from "./md-preview.js";

// Cache of the last file list from review_progress, so the filter box can
// re-render instantly against already-known data instead of waiting on a
// fresh server round-trip for every keystroke.
let allFiles = [];
// "All files" explore mode (see explore-all-files-btn below and
// server.py's list_all_files/handle_explore_file/handle_explore_reply):
// exploreAllFilesMode picks which of allFiles/allRepoFiles
// renderFilteredFileList reads from; allRepoFiles is only populated once
// the toggle is actually switched on (no reason to enumerate the whole
// repo on every connection when most reviews never touch it).
let exploreAllFilesMode = false;
let allRepoFiles = null;

// Speaker icon appended to a .md file's pill (see renderFileTreeNode).
// Swaps to a stop affordance while that file is the one currently being
// read; clicking it then sends "stop" instead of starting a new read.
export function makeSpeakFileButton(filePath) {
  const isReading = state.speakingFilePath === filePath;
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "file-speak-btn" + (isReading ? " reading" : "");
  btn.innerHTML = iconHtml(isReading ? "circle-stop" : "volume-2");
  btn.title = isReading ? `Stop reading ${filePath}` : `Read ${filePath} aloud`;
  btn.setAttribute("aria-label", btn.title);
  btn.addEventListener("click", (e) => {
    e.stopPropagation(); // don't also trigger the pill's own jump_to_hunk
    if (isReading) {
      send("stop"); // the generic send() hook below stops the queue + clears the icon since state.speakingFilePath is set
    } else {
      setSpeakingFile(filePath);
      send("speak_file", { file_path: filePath });
    }
  });
  return btn;
}

// Preview icon, also on .md pills. Deliberately separate from the speaker
// button above rather than folded into it: opening a preview is silent and
// read-only, and the speaker icon's "just read it, don't touch the code
// pane" behavior is worth keeping exactly as it was.
export function makePreviewFileButton(filePath) {
  const active = !!state.mdPreview && state.mdPreview.filePath === filePath && state.codeViewMode === "md-preview";
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "file-preview-btn" + (active ? " active" : "");
  btn.innerHTML = iconHtml("book-open");
  btn.title = `Preview ${filePath}`;
  btn.setAttribute("aria-label", btn.title);
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    send("open_md_preview", { file_path: filePath });
  });
  return btn;
}

// Files menu: one row per edited file, click to jump straight to its first
// hunk instead of stepping through with Next. Re-rendered (from a cached
// copy — see allFiles) on every review_progress message, so counts and the
// active-file highlight stay current after every hunk transition, toggle,
// or post-edit refresh, and also on every filter-box keystroke without a
// server round-trip.
// reviewStarted comes straight off this same review_progress payload (see
// send_review_progress in web/progress.py) rather than a side-channel synced
// from "presenting" — before the review starts, "0 reviewed" isn't a
// meaningful fraction of anything yet, so the label shows just the changed
// count instead of a misleading "(0/21)".
export function renderFileList(files, reviewStarted) {
  allFiles = files;
  const fullyReviewed = files.filter((f) => f.reviewed_count === f.hunk_count).length;
  // Nothing changed: the heading goes away entirely rather than standing
  // over an empty list announcing a category with no members. The "All
  // files" toggle beside it stays — with no diff to review, browsing the
  // repo is the only thing left to do here, so it's the one control that
  // still means something (it keeps its right-hand position via
  // .file-list-header's own last-child rule in style.css).
  filesMenuLabel.classList.toggle("hidden", !files.length);
  filesMenuLabel.textContent = reviewStarted
    ? `Changed files (${fullyReviewed}/${files.length})`
    : `Changed files (${files.length})`;
  renderFilteredFileList();
}

// Reply to "list_all_files" (see setExploreAllFilesMode below) — cached
// separately from allFiles (never merged into it) so renderFileList's own
// fullyReviewed count above stays computed only over real changed files,
// never diluted by explore-mode entries.
export function onAllFiles(payload) {
  allRepoFiles = payload.files;
  if (exploreAllFilesMode) renderFilteredFileList();
}

// The one place exploreAllFilesMode actually changes — an either/or
// toggle button (see index.html), not a checkbox: icon stays the same,
// .active/aria-pressed/the tooltip text are what show which state it's
// in, matching .file-preview-btn's own on/off treatment elsewhere in this
// sidebar rather than a form-control look for what's really a mode switch.
export function setExploreAllFilesMode(on) {
  exploreAllFilesMode = on;
  exploreAllFilesBtn.classList.toggle("active", on);
  exploreAllFilesBtn.setAttribute("aria-pressed", String(on));
  const label = on
    ? "Showing all files — click to show changed files only"
    : "Show all files — browse and ask about code that hasn't changed";
  exploreAllFilesBtn.title = label;
  exploreAllFilesBtn.setAttribute("aria-label", label);
  if (on) send("list_all_files", {});
  renderFilteredFileList();
}

// One-way "snap back to changed-files-only" — called whenever the review
// starts or ends (see onPresenting), not just from the button itself.
// Self-guarding so onPresenting can call this unconditionally on every
// hunk transition post-start without it doing anything once already off.
export function exitExploreAllFilesMode() {
  if (exploreAllFilesMode) setExploreAllFilesMode(false);
}

exploreAllFilesBtn.addEventListener("click", () => setExploreAllFilesMode(!exploreAllFilesMode));

// exploreAllFilesMode picks allRepoFiles (the whole repo, from
// list_all_files/"all_files" — see onAllFiles) over allFiles (changed
// files only, from review_progress) as the source this renders from.
export function renderFilteredFileList() {
  const source = exploreAllFilesMode ? allRepoFiles || [] : allFiles;
  const filter = fileFilterInput.value.trim().toLowerCase();
  const filtered = filter ? source.filter((f) => f.file_path.toLowerCase().includes(filter)) : source;
  fileListItemsEl.innerHTML = "";
  if (!filtered.length) {
    const empty = document.createElement("div");
    empty.className = "file-list-empty";
    empty.textContent = source.length
      ? "No files match."
      : exploreAllFilesMode && allRepoFiles === null
      ? "Loading files..."
      : "No files to review.";
    fileListItemsEl.appendChild(empty);
    return;
  }
  renderFileTreeNode(buildFileTree(filtered), fileListItemsEl, 0, !!filter);
  // Keep the active file's pill in view as navigation moves through hunks —
  // without this, a reviewer stepping past the visible slice of a long file
  // list has no way to tell which file they're on without scrolling to hunt
  // for the highlight themselves. "nearest" is a no-op when it's already
  // visible, so this doesn't fight manual scrolling on every re-render.
  if (!fileSidebarEl.classList.contains("collapsed")) {
    const activePill = fileListItemsEl.querySelector(".file-pill.active");
    if (activePill) activePill.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }
}

// Groups the flat file_path list into a folder tree (split on "/") so the
// sidebar reads like a file explorer instead of a list of repeated path
// prefixes — e.g. "app/server.py" and "app/diff_service.py" nest under one
// "app" header rather than each spelling out "app/" in full. Filtering
// (see renderFilteredFileList) still matches against the full path, so a
// folder only appears here at all if something inside it matched. Each
// folder node's own "path" (its full slash-joined path from the root, not
// just its own name) is what identifies it in collapsedFolders below —
// two different folders can share a bare name ("utils"), but never a
// full path.
function buildFileTree(files) {
  const root = { folders: new Map(), files: [] };
  for (const f of files) {
    const parts = f.file_path.split("/");
    const baseName = parts.pop();
    let node = root;
    let path = "";
    for (const part of parts) {
      path = path ? `${path}/${part}` : part;
      if (!node.folders.has(part)) node.folders.set(part, { folders: new Map(), files: [], path });
      node = node.folders.get(part);
    }
    node.files.push({ ...f, baseName });
  }
  return root;
}

// Which folders (by full path — see buildFileTree) the reviewer has
// collapsed. Survives re-renders (review_progress updates, filter-box
// keystrokes, "All files" toggling) since it's read fresh from here every
// time rather than being baked into the tree data — collapsing a folder
// shouldn't spring back open just because a hunk was marked reviewed
// elsewhere. Not persisted to localStorage — resets on page reload, same
// as the file sidebar's own expand/collapse state already does.
let collapsedFolders = new Set();

// forceExpanded is true while a filter is active (see renderFilteredFileList) —
// a collapsed folder must not hide a search match sitting inside it, so
// filtering always renders every matching folder open regardless of
// collapsedFolders, without mutating it (clearing the filter box restores
// whatever was actually collapsed).
function renderFileTreeNode(node, container, depth, forceExpanded) {
  const indent = `${depth * 0.9}rem`;
  for (const name of [...node.folders.keys()].sort((a, b) => a.localeCompare(b))) {
    const child = node.folders.get(name);
    const expanded = forceExpanded || !collapsedFolders.has(child.path);
    const label = document.createElement("button");
    label.type = "button";
    label.className = "file-folder-label";
    label.style.paddingLeft = indent;
    label.setAttribute("aria-expanded", String(expanded));
    label.title = expanded ? `Collapse ${name}` : `Expand ${name}`;
    const chevron = makeIconSpan("chevron-right");
    chevron.classList.add("file-folder-chevron");
    if (expanded) chevron.classList.add("expanded");
    label.append(chevron, makeIconSpan("folder"), document.createTextNode(` ${name}`));
    label.addEventListener("click", () => {
      // Toggles the *real* state even while forceExpanded is visually
      // overriding it (filtering) — the reviewer's click still means
      // "collapse this," it just won't be visible until the filter clears.
      if (collapsedFolders.has(child.path)) collapsedFolders.delete(child.path);
      else collapsedFolders.add(child.path);
      renderFilteredFileList();
    });
    container.appendChild(label);

    const childContainer = document.createElement("div");
    childContainer.className = "file-folder-children" + (expanded ? "" : " hidden");
    container.appendChild(childContainer);
    renderFileTreeNode(child, childContainer, depth + 1, forceExpanded);
  }
  for (const f of [...node.files].sort((a, b) => a.baseName.localeCompare(b.baseName))) {
    const pill = document.createElement("button");
    pill.className = "file-pill" + (f.file_path === state.currentFilePath ? " active" : "");
    pill.style.paddingLeft = `calc(${indent} + 0.6rem)`;
    // hunk_count is null for a file "All files" mode added that has no
    // diff hunks (see handle_list_all_files in
    // handlers/explore.py) — no
    // reviewed/hunk counts to show, and clicking it opens it read-only
    // via explore_file instead of jumping to a hunk that doesn't exist.
    if (f.hunk_count === null) {
      pill.classList.add("file-pill-unchanged");
      pill.title = `Explore ${f.file_path} (no changes)`;
      pill.addEventListener("click", () => send("explore_file", { file_path: f.file_path }));
      pill.appendChild(document.createTextNode(f.baseName));
    } else {
      pill.title = `Jump to ${f.file_path}`;
      pill.addEventListener("click", () => send("jump_to_hunk", { index: f.first_index }));
      pill.appendChild(document.createTextNode(`${f.baseName} (${f.reviewed_count}/${f.hunk_count})`));
    }
    if (f.file_path.toLowerCase().endsWith(".md")) {
      pill.appendChild(makePreviewFileButton(f.file_path));
      pill.appendChild(makeSpeakFileButton(f.file_path));
    }
    container.appendChild(pill);
  }
}

// Shared edge-tab collapse/expand controller for the file sidebar and chat
// pane, which are otherwise identical: toggle a .collapsed class on the
// panel (that alone drives its width/padding transition — see .file-
// sidebar/.chat-pane in style.css), flip the tab's chevron and
// aria-expanded/aria-label, persist the choice, and restore it (falling
// back to each panel's own default) on load. tabEl now lives *inside*
// panelEl (see index.html) rather than in a separate floating or gutter
// element, so inert has to target contentEl — the panel's other child,
// wrapping everything except the tab itself (#file-list, #chat-pane-
// content) — instead of panelEl as a whole: inert-ing the panel itself
// would take the button meant to *reopen* it out of the tab order and
// accessibility tree right along with it, a self-locking control. inert
// on contentEl while collapsed keeps its now-hidden-behind-a-sliver
// controls out of both, and per spec moves focus out automatically if
// something inside it happened to still be focused when it collapses.
function setupEdgeTab({ panelEl, contentEl, tabEl, tabIconEl, storageKey, defaultOpen, expandedIcon, collapsedIcon, expandLabel, collapseLabel }) {
  function setOpen(open) {
    panelEl.classList.toggle("collapsed", !open);
    contentEl.toggleAttribute("inert", !open);
    tabEl.setAttribute("aria-expanded", String(open));
    tabEl.setAttribute("aria-label", open ? collapseLabel : expandLabel);
    tabIconEl.setAttribute("href", open ? expandedIcon : collapsedIcon);
    try {
      localStorage.setItem(storageKey, open ? "1" : "0");
    } catch {
      // non-critical, ignore
    }
  }

  tabEl.addEventListener("click", () => {
    setOpen(panelEl.classList.contains("collapsed"));
  });

  let open = defaultOpen;
  try {
    const saved = localStorage.getItem(storageKey);
    if (saved !== null) open = saved === "1";
  } catch {
    // non-critical, ignore
  }
  setOpen(open);

  return setOpen;
}

const FILES_MENU_OPEN_KEY = "ai_pear_review_files_menu_open";
const CHAT_PANE_OPEN_KEY = "ai_pear_review_chat_pane_open";

// Captured (rather than left as fire-and-forget calls) so the guided tour
// can reveal a collapsed panel for its own step and put it back afterward,
// via the exact same open/close logic — aria state, icon, and the
// localStorage persistence — everything else that toggles these panels
// already goes through, instead of a second, parallel way to open them.
export const setFileSidebarOpen = setupEdgeTab({
  panelEl: fileSidebarEl,
  contentEl: fileListEl,
  tabEl: fileSidebarTab,
  tabIconEl: fileSidebarTabIcon,
  storageKey: FILES_MENU_OPEN_KEY,
  defaultOpen: false, // matches today's default: file explorer starts collapsed
  expandedIcon: "#icon-chevron-left", // points toward the left screen edge — the direction clicking will collapse it
  collapsedIcon: "#icon-chevron-right", // points back into the content — the direction clicking will expand it
  expandLabel: "Expand file explorer",
  collapseLabel: "Collapse file explorer",
});

setupEdgeTab({
  panelEl: chatPaneEl,
  contentEl: chatPaneContentEl,
  tabEl: chatPaneTab,
  tabIconEl: chatPaneTabIcon,
  storageKey: CHAT_PANE_OPEN_KEY,
  defaultOpen: true, // matches today's default: chat pane starts open/always-visible
  expandedIcon: "#icon-chevron-right", // points toward the right screen edge — the direction clicking will collapse it
  collapsedIcon: "#icon-chevron-left", // points back into the content — the direction clicking will expand it
  expandLabel: "Expand chat",
  collapseLabel: "Collapse chat",
});

fileFilterInput.addEventListener("input", renderFilteredFileList);
