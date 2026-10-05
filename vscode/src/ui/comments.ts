import * as vscode from "vscode";

import type { Backend } from "../backend/backend.ts";
import type { ReviewComment, Severity } from "../backend/protocol.ts";
import { reviewLocation, reviewUri } from "../git.ts";
import { prReviewFor } from "../github/prReviews.ts";
import { log, showError } from "../log.ts";
import { anchorRange } from "../review/anchors.ts";
import { publish } from "../testProbe.ts";
import { showPreview } from "./previewTabs.ts";
import type { SelectionContext } from "./selection.ts";

export interface Comments {
  readonly count: number;
  // The queued comments, oldest first: what a pull request review posts.
  readonly pending: ReviewComment[];
  readonly onDidChangeCount: vscode.Event<number>;
  // A recording started from a comment box's mic belongs to that box: voice.ts hands
  // its audio here, and this queues it as the comment. False when no box is waiting.
  takeVoiceComment(audio_base64: string): boolean;
}

const SEVERITY_LABEL: Record<Severity, string> = { "must-fix": "Must fix", suggestion: "Suggestion", nit: "Nit" };

class PearComment implements vscode.Comment {
  body: string | vscode.MarkdownString;
  mode = vscode.CommentMode.Preview;
  author: vscode.CommentAuthorInformation;
  contextValue = "pearComment";

  constructor(
    public data: ReviewComment,
    public thread?: vscode.CommentThread,
  ) {
    this.body = data.instruction;
    this.author = { name: SEVERITY_LABEL[data.severity] };
  }

  update(data: ReviewComment): void {
    this.data = data;
    this.body = data.instruction;
    this.author = { name: SEVERITY_LABEL[data.severity] };
    this.mode = vscode.CommentMode.Preview;
  }
}

// Review comments as native comment threads. The backend's queue is the source of
// truth: a draft thread is sent as request_change and disposed, and the thread that
// stays is drawn from the server's review_comment_queued (or review_comments_sync on
// connect), so the two can't disagree.
export function register(
  backend: Backend,
  selection: SelectionContext,
): { comments: Comments; disposables: vscode.Disposable[] } {
  const controller = vscode.comments.createCommentController("pearReview", "Pear Review");
  // A pull request review's comments go to GitHub, not to a coding agent.
  const setPrompt = (): void => {
    const pr = prReviewFor(backend.repoPath);
    controller.options = pr
      ? { prompt: `Comment on PR #${pr.number}`, placeHolder: "What should change here?" }
      : { prompt: "Comment for the coding agent", placeHolder: "What should change here?" };
  };
  setPrompt();
  const threads = new Map<number, PearComment>();
  // The backend's queue as last heard, kept apart from the threads: a comment whose
  // thread couldn't be drawn is still queued, and still posted.
  const pending = new Map<number, ReviewComment>();
  publish("comments.threads", () =>
    [...threads.values()].map((c) => ({
      id: c.data.id,
      uri: c.thread?.uri.toString(),
      startLine: (c.thread?.range?.start.line ?? -1) + 1,
      endLine: (c.thread?.range?.end.line ?? -1) + 1,
      body: typeof c.body === "string" ? c.body : c.body.value,
      author: c.author.name,
      severity: c.data.severity,
      comment: c,
    })),
  );
  const countChanges = new vscode.EventEmitter<number>();
  // The comment box whose mic was pressed, until its recording comes back. A recording
  // started anywhere else (the chat's mic, the keybinding) clears it.
  let voiceThread: vscode.CommentThread | undefined;
  let voiceArmed = false;
  let recordingNow = false;
  // A mic press whose recording never starts (voice input off, no backend) lapses, so
  // a later recording from the chat isn't taken for a comment.
  const ARM_TIMEOUT_MS = 5_000;
  let count = 0;
  let open = false;
  let reviewFiles = new Set<string>();

  const setCount = (value: number): void => {
    count = value;
    countChanges.fire(value);
  };

  // Comments are allowed on review files while the review is running, as in the browser.
  // Reassigning the provider is what makes VS Code ask it again.
  const refreshRanges = (): void => {
    controller.commentingRangeProvider = {
      provideCommentingRanges(document) {
        const root = backend.repoPath;
        const location = root ? reviewLocation(document.uri, root) : undefined;
        if (!open || !location || !reviewFiles.has(location.filePath)) return [];
        return [new vscode.Range(0, 0, Math.max(document.lineCount - 1, 0), 0)];
      },
    };
  };

  // Bumped by clearThreads: a show() still awaiting its URI from before the clear (a
  // sync, a finished plan) must not draw a thread afterwards.
  let epoch = 0;

  const clearThreads = (): void => {
    epoch += 1;
    for (const comment of threads.values()) comment.thread?.dispose();
    threads.clear();
  };

  const show = async (data: ReviewComment): Promise<void> => {
    const root = backend.repoPath;
    if (!root) return;
    const started = epoch;
    const range = anchorRange(data.anchor);
    const uri = await reviewUri(root, data.file_path, range.side);
    if (started !== epoch) return;
    threads.get(data.id)?.thread?.dispose();
    const comment = new PearComment(data);
    const thread = controller.createCommentThread(uri, new vscode.Range(range.startLine - 1, 0, range.endLine - 1, 0), [
      comment,
    ]);
    const pr = prReviewFor(root);
    thread.label = `${data.where} · ${pr ? `goes to PR #${pr.number}` : "goes into the plan"}`;
    thread.canReply = false;
    thread.contextValue = "pearThread";
    thread.collapsibleState = vscode.CommentThreadCollapsibleState.Expanded;
    comment.thread = thread;
    threads.set(data.id, comment);
  };

  const showAll = (list: ReviewComment[]): void => {
    for (const data of list) show(data).catch((err: unknown) => log(`Couldn't show a comment: ${String(err)}`));
  };

  const redraw = (comment: PearComment): void => {
    if (comment.thread) comment.thread.comments = [...comment.thread.comments];
  };

  const submit = async (reply: vscode.CommentReply, severity: Severity): Promise<void> => {
    const root = backend.repoPath;
    const text = reply.text.trim();
    const location = root ? reviewLocation(reply.thread.uri, root) : undefined;
    if (!text || !location || !reply.thread.range) return;
    const document = await vscode.workspace.openTextDocument(reply.thread.uri);
    // A thread's range is whole lines, its end line included even at column 0; the
    // editor-selection rule in selection.ts's lineSpan would drop that last line.
    const { start, end } = reply.thread.range;
    const span = { startLine: start.line + 1, endLine: end.line + 1 };
    const marked_lines = selection.markedLinesAt({ ...location, ...span }, document);
    backend.send("request_change", { text, marked_lines, severity });
    reply.thread.dispose();
  };

  // A spoken comment: the transcription becomes the instruction (fix its wording with
  // the thread's edit), as a Suggestion (re-tag it with the thread's tag button).
  const submitVoice = async (thread: vscode.CommentThread, audio_base64: string): Promise<void> => {
    const root = backend.repoPath;
    const location = root ? reviewLocation(thread.uri, root) : undefined;
    if (!location || !thread.range) return;
    const document = await vscode.workspace.openTextDocument(thread.uri);
    const { start, end } = thread.range;
    const span = { startLine: start.line + 1, endLine: end.line + 1 };
    const marked_lines = selection.markedLinesAt({ ...location, ...span }, document);
    backend.send("request_change", { audio_base64, marked_lines, severity: "suggestion" });
    thread.dispose();
  };

  const guarded =
    <A extends unknown[]>(fn: (...args: A) => unknown) =>
    (...args: A): void => {
      try {
        const result = fn(...args);
        if (result instanceof Promise) result.catch((err: unknown) => showError(String(err)));
      } catch (err) {
        showError(err instanceof Error ? err.message : String(err));
      }
    };

  // `preset` skips the prompts: { note?, asSkill? }, from a keybinding or the tests.
  const createPlan = async (preset?: unknown): Promise<void> => {
    if (prReviewFor(backend.repoPath)) {
      await vscode.commands.executeCommand("pearReview.submitPullRequestReview");
      return;
    }
    const given =
      typeof preset === "object" && preset !== null ? (preset as { note?: unknown; asSkill?: unknown }) : undefined;
    if (count === 0) {
      void vscode.window.showInformationMessage(
        "No review comments yet. Hover the diff's gutter and press + to comment on lines.",
      );
      return;
    }
    const note = given
      ? typeof given.note === "string"
        ? given.note
        : ""
      : await vscode.window.showInputBox({
          title: `Create plan from ${count} comment${count === 1 ? "" : "s"}`,
          prompt: "An overall note for the coding agent (optional)",
          ignoreFocusOut: true,
        });
    if (note === undefined) return;
    const form = given
      ? { asSkill: given.asSkill === true }
      : await vscode.window.showQuickPick(
          [
            { label: "Plan", description: "Write .review/review_<time>.md", asSkill: false },
            { label: "Plan and /apply-review skill", description: "Also save it as an agent skill", asSkill: true },
          ],
          { title: "Create plan", ignoreFocusOut: true },
        );
    if (!form) return;
    backend.send(
      "finish_review",
      note.trim() ? { note: note.trim(), as_skill: form.asSkill } : { as_skill: form.asSkill },
    );
  };

  refreshRanges();
  return {
    comments: {
      get count() {
        return count;
      },
      get pending() {
        return [...pending.values()].sort((a, b) => a.id - b.id);
      },
      onDidChangeCount: countChanges.event,
      takeVoiceComment(audio_base64) {
        const thread = voiceThread;
        voiceThread = undefined;
        if (!thread) return false;
        submitVoice(thread, audio_base64).catch((err: unknown) => showError(String(err)));
        return true;
      },
    },
    disposables: [
      backend.on("recording_state", ({ recording }) => {
        recordingNow = recording;
        if (recording && !voiceArmed) voiceThread = undefined;
        voiceArmed = false;
      }),
      // A recording that fails (too short, no mic) leaves no audio; the box stays open.
      backend.on("error", () => {
        if (voiceThread && !voiceArmed && !recordingNow) voiceThread = undefined;
      }),
      vscode.commands.registerCommand("pearReview.comment.voiceStart", (thread: vscode.CommentThread) => {
        voiceThread = thread;
        voiceArmed = true;
        setTimeout(() => {
          if (!voiceArmed) return;
          voiceArmed = false;
          voiceThread = undefined;
        }, ARM_TIMEOUT_MS);
        void vscode.commands.executeCommand("pearReview.toggleRecording");
      }),
      vscode.commands.registerCommand("pearReview.comment.voiceStop", () => {
        void vscode.commands.executeCommand("pearReview.toggleRecording");
      }),
      controller,
      countChanges,
      backend.on("review_progress", (p) => {
        const nowOpen = p.review_started && !p.review_ended;
        const files = new Set(p.files.map((f) => f.file_path));
        const changed =
          nowOpen !== open || files.size !== reviewFiles.size || [...files].some((f) => !reviewFiles.has(f));
        open = nowOpen;
        reviewFiles = files;
        if (changed) refreshRanges();
      }),
      backend.on("review_comments_sync", ({ comments }) => {
        pending.clear();
        for (const c of comments) pending.set(c.id, c);
        clearThreads();
        showAll(comments);
        setCount(comments.length);
      }),
      backend.on("review_comment_queued", (data) => {
        pending.set(data.id, data);
        showAll([data]);
        setCount(data.pending_count);
      }),
      backend.on("review_comment_updated", ({ id, instruction, severity }) => {
        const queued = pending.get(id);
        if (queued) pending.set(id, { ...queued, instruction, severity });
        const comment = threads.get(id);
        if (!comment) return;
        comment.update({ ...comment.data, instruction, severity });
        redraw(comment);
      }),
      backend.on("review_comment_removed", ({ id, pending_count }) => {
        pending.delete(id);
        threads.get(id)?.thread?.dispose();
        threads.delete(id);
        setCount(pending_count);
      }),
      backend.on("review_finished", (result) => {
        pending.clear();
        clearThreads();
        setCount(0);
        void vscode.window
          .showInformationMessage(
            `Plan written to ${result.plan_file} (${result.comment_count} comment${result.comment_count === 1 ? "" : "s"}). ` +
              `Give your coding agent: ${result.instruction_line}` +
              (result.skill_note ? ` — ${result.skill_note}` : ""),
            "Open plan",
            "Read aloud",
            "Copy instruction",
          )
          .then((choice) => {
            if (choice === "Open plan") {
              void showPreview(vscode.Uri.file(result.plan_path));
            } else if (choice === "Read aloud") {
              void vscode.commands.executeCommand("pearReview.readAloud", vscode.Uri.file(result.plan_path));
            } else if (choice === "Copy instruction") {
              void vscode.env.clipboard.writeText(result.instruction_line);
            }
          });
      }),
      backend.onStateChange((state) => {
        if (state === "ready") {
          setPrompt();
          return;
        }
        voiceThread = undefined;
        pending.clear();
        clearThreads();
        setCount(0);
        open = false;
        refreshRanges();
      }),
      vscode.commands.registerCommand(
        "pearReview.comment.mustFix",
        guarded((reply: vscode.CommentReply) => submit(reply, "must-fix")),
      ),
      vscode.commands.registerCommand(
        "pearReview.comment.suggestion",
        guarded((reply: vscode.CommentReply) => submit(reply, "suggestion")),
      ),
      vscode.commands.registerCommand(
        "pearReview.comment.nit",
        guarded((reply: vscode.CommentReply) => submit(reply, "nit")),
      ),
      vscode.commands.registerCommand("pearReview.comment.edit", (comment: PearComment) => {
        comment.mode = vscode.CommentMode.Editing;
        redraw(comment);
      }),
      vscode.commands.registerCommand(
        "pearReview.comment.save",
        guarded((comment: PearComment) => {
          const instruction = (typeof comment.body === "string" ? comment.body : comment.body.value).trim();
          if (!instruction) return;
          backend.send("edit_change_request", { id: comment.data.id, instruction });
        }),
      ),
      vscode.commands.registerCommand("pearReview.comment.cancel", (comment: PearComment) => {
        comment.update(comment.data);
        redraw(comment);
      }),
      vscode.commands.registerCommand(
        "pearReview.comment.severity",
        guarded(async (comment: PearComment) => {
          const picked = await vscode.window.showQuickPick(
            (Object.keys(SEVERITY_LABEL) as Severity[]).map((s) => ({
              label: SEVERITY_LABEL[s],
              severity: s,
              picked: s === comment.data.severity,
            })),
            { title: "Comment severity" },
          );
          if (picked) {
            backend.send("edit_change_request", {
              id: comment.data.id,
              instruction: comment.data.instruction,
              severity: picked.severity,
            });
          }
        }),
      ),
      vscode.commands.registerCommand(
        "pearReview.comment.delete",
        guarded((comment: PearComment) => backend.send("remove_review_comment", { id: comment.data.id })),
      ),
      vscode.commands.registerCommand("pearReview.createPlan", guarded(createPlan)),
    ],
  };
}
