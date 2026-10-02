import * as vscode from "vscode";

import type { Backend } from "../backend/backend.ts";
import type { ReviewComment, Severity } from "../backend/protocol.ts";
import { reviewLocation, reviewUri } from "../git.ts";
import { log, showError } from "../log.ts";
import { anchorRange } from "../review/anchors.ts";
import { lineSpan, type SelectionContext } from "./selection.ts";

export interface Comments {
  readonly count: number;
  readonly onDidChangeCount: vscode.Event<number>;
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
  controller.options = { prompt: "Comment for the coding agent", placeHolder: "What should change here?" };
  const threads = new Map<number, PearComment>();
  const countChanges = new vscode.EventEmitter<number>();
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

  const clearThreads = (): void => {
    for (const comment of threads.values()) comment.thread?.dispose();
    threads.clear();
  };

  const show = async (data: ReviewComment): Promise<void> => {
    const root = backend.repoPath;
    if (!root) return;
    threads.get(data.id)?.thread?.dispose();
    const range = anchorRange(data.anchor);
    const uri = await reviewUri(root, data.file_path, range.side);
    const comment = new PearComment(data);
    const thread = controller.createCommentThread(uri, new vscode.Range(range.startLine - 1, 0, range.endLine - 1, 0), [
      comment,
    ]);
    thread.label = `${data.where} · goes into the plan`;
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
    const marked_lines = selection.markedLinesAt({ ...location, ...lineSpan(reply.thread.range) }, document);
    backend.send("request_change", { text, marked_lines, severity });
    reply.thread.dispose();
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

  const createPlan = async (): Promise<void> => {
    if (count === 0) {
      void vscode.window.showInformationMessage(
        "No review comments yet. Hover the diff's gutter and press + to comment on lines.",
      );
      return;
    }
    const note = await vscode.window.showInputBox({
      title: `Create plan from ${count} comment${count === 1 ? "" : "s"}`,
      prompt: "An overall note for the coding agent (optional)",
      ignoreFocusOut: true,
    });
    if (note === undefined) return;
    const form = await vscode.window.showQuickPick(
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
      onDidChangeCount: countChanges.event,
    },
    disposables: [
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
        clearThreads();
        showAll(comments);
        setCount(comments.length);
      }),
      backend.on("review_comment_queued", (data) => {
        showAll([data]);
        setCount(data.pending_count);
      }),
      backend.on("review_comment_updated", ({ id, instruction, severity }) => {
        const comment = threads.get(id);
        if (!comment) return;
        comment.update({ ...comment.data, instruction, severity });
        redraw(comment);
      }),
      backend.on("review_comment_removed", ({ id, pending_count }) => {
        threads.get(id)?.thread?.dispose();
        threads.delete(id);
        setCount(pending_count);
      }),
      backend.on("review_finished", (result) => {
        clearThreads();
        setCount(0);
        void vscode.window
          .showInformationMessage(
            `Plan written to ${result.plan_file} (${result.comment_count} comment${result.comment_count === 1 ? "" : "s"}). ` +
              `Give your coding agent: ${result.instruction_line}` +
              (result.skill_note ? ` — ${result.skill_note}` : ""),
            "Open plan",
            "Copy instruction",
          )
          .then((choice) => {
            if (choice === "Open plan") {
              void vscode.commands.executeCommand("markdown.showPreview", vscode.Uri.file(result.plan_path));
            } else if (choice === "Copy instruction") {
              void vscode.env.clipboard.writeText(result.instruction_line);
            }
          });
      }),
      backend.onStateChange((state) => {
        if (state === "ready") return;
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
