// The WebSocket contract, typed. The source of truth is backend/docs/wire-protocol.md;
// test/unit/protocol.test.ts fails if a message there has no entry here, or the reverse.
//
// Payloads are typed in full where the extension reads or sends them. A message no UI
// uses yet is `Unknown` until one does — typed then, against the handler's docstring.

type Empty = Record<string, never>;
type Unknown = Record<string, unknown>;
type Voiced = { text: string } | { audio_base64: string };

// A line the reviewer selected as context for a question (app/web/context.py's
// "marked_lines"). The line text comes from the client.
export interface MarkedLine {
  file_path: string;
  old_lineno: number | null;
  new_lineno: number | null;
  text: string;
  kind: "context" | "add" | "del";
}

export interface ClientPayloads {
  next: Empty;
  prev: Empty;
  stop: Empty;
  jump_to_hunk: { index: number };
  start_review: Empty;
  end_review: Empty;
  show_summary: Empty;
  new_review: Empty;
  toggle_reviewed: Empty;
  toggle_reviewed_all: Empty;
  refresh_diff: Empty;
  reply: Voiced & { marked_lines?: MarkedLine[] };
  request_change: Voiced & { marked_lines?: MarkedLine[]; severity?: Severity };
  edit_change_request: { id: number; instruction: string; severity?: Severity };
  remove_review_comment: { id: number };
  finish_review: { note?: string; as_skill?: boolean };
  act_now: Voiced & { marked_lines?: MarkedLine[] };
  refine_act_now: { text: string };
  confirm_act_now: Empty;
  step_into: { text: string };
  list_all_files: Empty;
  explore_file: { file_path: string };
  explore_reply: Voiced & { file_path: string };
  look_deeper: { index: number; question?: string };
  get_settings: { provider?: string };
  set_settings: { settings: Unknown };
  set_voice_prefs: { stt_enabled?: boolean; tts_enabled?: boolean };
  open_md_preview: { file_path: string };
  speak_file: { file_path: string; start_line?: number; end_line?: number };
  speak_turn: { text: string };
  explain_hunk: { index: number };
  set_narration_prefs: { auto_narrate: boolean };
  speak_text: { text: string };
  start_recording: Empty;
  stop_recording: Empty;
}

// One line of a file's whole diff against HEAD (diff_service.Hunk.full_lines).
export interface DiffLine {
  kind: "context" | "add" | "del";
  old_lineno: number | null;
  new_lineno: number | null;
  text: string;
}

// `done: true` carries only index/total (and the end-of-review fields); a real hunk
// carries the rest.
export interface Presenting {
  index: number;
  total: number;
  done: boolean;
  file_path?: string;
  header?: string;
  full_lines?: DiffLine[];
  // Inclusive index range into full_lines that this hunk's changed lines cover.
  highlight_start?: number;
  highlight_end?: number;
  review_started?: boolean;
  review_ended?: boolean;
  narrating?: boolean;
  narration_available?: boolean;
}

// A rendered markdown block of a reply (markdown_speech.block_to_payload).
export interface Span {
  text: string;
  style: string;
  href?: string;
}

export interface Block {
  kind: string;
  level: number;
  spans: Span[];
  code_text?: string;
  code_lang?: string | null;
  ordered?: boolean;
  marker?: string | null;
  cells?: string[];
  is_header?: boolean;
}

export interface RelatedHunk {
  index: number;
  file_path: string;
  relation: string;
  note?: string | null;
}

// narration, human_turn and reviewer_turn. A turn about a hunk carries which one;
// a persona reply carries its render-ready blocks and the text to speak.
export interface Turn {
  text: string;
  spoken?: string;
  blocks?: Block[];
  related?: RelatedHunk[];
  index?: number;
  total?: number;
  file_path?: string;
  // Only on narration: false is degraded mode's placeholder text, not the persona.
  narration_available?: boolean;
}

export interface DeeperTurn extends Turn {
  agent: string;
  provider?: string | null;
  model?: string | null;
}

// comments.py's COMMENT_SEVERITIES.
export type Severity = "must-fix" | "suggestion" | "nit";

// Where a queued comment sits (web/context.py's line_context); null when it covers a
// whole hunk rather than marked lines.
export interface CommentAnchor {
  first_old_lineno: number | null;
  first_new_lineno: number | null;
  last_old_lineno: number | null;
  last_new_lineno: number | null;
}

export interface ReviewComment {
  id: number;
  file_path: string;
  where: string;
  instruction: string;
  severity: Severity;
  anchor: CommentAnchor | null;
}

export interface ReviewFinished {
  plan_path: string;
  plan_file: string;
  as_skill: boolean;
  skill_written: boolean;
  skill_note: string | null;
  instruction_line: string;
  comment_count: number;
}

// One file of an Act Now proposal (act_now.py's _preview_entry): its whole diff from
// the working file to what the agent wrote.
export interface ProposedFile {
  file_path: string;
  status: "modified" | "added" | "deleted";
  full_lines: DiffLine[];
  highlight_start: number;
  highlight_end: number;
}

export interface ActNowPreview extends ProposedFile {
  files: ProposedFile[];
  summary: string;
  agent: string;
}

// get_settings / set_settings's reply (settings.py). Only what the extension reads is
// typed; the conversation block's provider sections stay open.
export interface Settings {
  settings: { provider?: string; [section: string]: unknown };
  installed_models?: string[];
  harness_settings: {
    agent: string;
    agents: string[];
    provider: string | null;
    model: string | null;
    model_source?: string | null;
  };
  saved?: boolean;
  applies_on_reconnect?: boolean;
}

export interface ActNowStatus {
  available: boolean;
  detail: string;
  agent?: string;
}

export interface AudioChunk {
  audio_base64: string;
  mime_type: string;
  chunk_index: number;
  chunk_count: number;
}

export interface ProgressHunk {
  index: number;
  header: string;
  reviewed: boolean;
}

export interface ProgressFile {
  file_path: string;
  hunk_count: number;
  reviewed_count: number;
  first_index: number;
  hunks: ProgressHunk[];
}

export interface ReviewProgress {
  reviewed_count: number;
  total: number;
  current_reviewed: boolean;
  files: ProgressFile[];
  review_started: boolean;
  review_ended: boolean;
}

export interface ServiceStatus {
  stt?: boolean;
  tts?: boolean;
  llm?: boolean;
  briefing?: boolean;
  // Look deeper runs on the same coding agent as Act Now.
  act_now?: ActNowStatus;
}

export interface ServerPayloads {
  presenting: Presenting;
  narration: Turn;
  human_turn: Turn;
  reviewer_turn: Turn;
  deeper_turn: DeeperTurn;
  audio_chunk: AudioChunk;
  turn_audio_chunk: AudioChunk;
  tour_audio_chunk: Unknown;
  file_audio_chunk: Unknown;
  md_preview: Unknown;
  all_files: Unknown;
  file_explore: Unknown;
  definition: Unknown;
  review_progress: ReviewProgress;
  review_comments_sync: { comments: ReviewComment[] };
  review_comment_queued: ReviewComment & { pending_count: number };
  review_comment_updated: { id: number; instruction: string; severity: Severity };
  review_comment_removed: { id: number; pending_count: number };
  review_finished: ReviewFinished;
  act_now_preview: ActNowPreview;
  act_now_cleared: { message: string };
  agent_stopped: { kind: string; message: string };
  settings: Settings;
  recording_state: { recording: boolean };
  recording_result: { audio_base64: string; mime_type: string; duration_seconds: number };
  context_too_large: Unknown;
  service_status: ServiceStatus;
  notice: { message: string; level?: "info" | "success" };
  error: { message: string };
}

export type ClientMessageType = keyof ClientPayloads;
export type ServerMessageType = keyof ServerPayloads;

export type ServerMessage = {
  [K in ServerMessageType]: { type: K; payload: ServerPayloads[K] };
}[ServerMessageType];

// Runtime lists for the completeness test. `satisfies` makes the compiler hold each
// list to exactly the keys of its interface — a missing or extra name is a type error.
export const CLIENT_MESSAGE_TYPES = Object.keys({
  next: true,
  prev: true,
  stop: true,
  jump_to_hunk: true,
  start_review: true,
  end_review: true,
  show_summary: true,
  new_review: true,
  toggle_reviewed: true,
  toggle_reviewed_all: true,
  refresh_diff: true,
  reply: true,
  request_change: true,
  edit_change_request: true,
  remove_review_comment: true,
  finish_review: true,
  act_now: true,
  refine_act_now: true,
  confirm_act_now: true,
  step_into: true,
  list_all_files: true,
  explore_file: true,
  explore_reply: true,
  look_deeper: true,
  get_settings: true,
  set_settings: true,
  set_voice_prefs: true,
  open_md_preview: true,
  speak_file: true,
  speak_turn: true,
  explain_hunk: true,
  set_narration_prefs: true,
  speak_text: true,
  start_recording: true,
  stop_recording: true,
} satisfies Record<ClientMessageType, true>);

export const SERVER_MESSAGE_TYPES = Object.keys({
  presenting: true,
  narration: true,
  human_turn: true,
  reviewer_turn: true,
  deeper_turn: true,
  audio_chunk: true,
  turn_audio_chunk: true,
  tour_audio_chunk: true,
  file_audio_chunk: true,
  md_preview: true,
  all_files: true,
  file_explore: true,
  definition: true,
  review_progress: true,
  review_comments_sync: true,
  review_comment_queued: true,
  review_comment_updated: true,
  review_comment_removed: true,
  review_finished: true,
  act_now_preview: true,
  act_now_cleared: true,
  agent_stopped: true,
  settings: true,
  recording_state: true,
  recording_result: true,
  context_too_large: true,
  service_status: true,
  notice: true,
  error: true,
} satisfies Record<ServerMessageType, true>);

export function isServerMessage(value: unknown): value is ServerMessage {
  if (typeof value !== "object" || value === null) return false;
  const { type, payload } = value as { type?: unknown; payload?: unknown };
  return (
    typeof type === "string" &&
    SERVER_MESSAGE_TYPES.includes(type) &&
    typeof payload === "object" &&
    payload !== null
  );
}
