# Wire protocol — browser ⟷ server

Every WebSocket message is `{"type": ..., "payload": {...}}` over `/ws`
([app/server.py](../app/server.py)). This page is the **contract only**: the
message name, what it carries, and which function owns it.

**Why a message behaves the way it does belongs in that handler's
docstring, not here.** This file exists because the contract spans Python
and the browser modules in [static/js/](../static/js/), so neither side can state it alone. It
drifted badly once already by carrying the rationale too — the prose and
the code it described were in different files and stopped matching. Add a
row here; put the explanation next to the code.

Inbound messages are registered with the `@handler` decorator and
dispatched through [app/handlers/registry.py](../app/handlers/registry.py).
`cancels` means `cancel_current` runs before the handler; `background`
means it runs as `session.current_task` rather than inline.
`test_wire_protocol_doc_matches_the_registry` in
[tests/test_handler_registry.py](../tests/test_handler_registry.py) fails if
this table and the registry disagree.

## Browser → Python

| Message | Payload | Handler | cancels | background |
|---|---|---|---|---|
| `next` | — | `review_flow.handle_next` | yes | — |
| `prev` | — | `review_flow.handle_prev` | yes | — |
| `stop` | — | `review_flow.handle_stop` | yes | — |
| `jump_to_hunk` | `index` | `review_flow.handle_jump_to_hunk` | yes | — |
| `start_review` | — | `review_flow.handle_start_review` | — | — |
| `end_review` | — | `review_flow.handle_end_review` | — | — |
| `show_summary` | — | `review_flow.handle_show_summary` | — | — |
| `new_review` | — | `review_flow.handle_new_review` | — | — |
| `toggle_reviewed` | — | `review_flow.handle_toggle_reviewed` | — | — |
| `toggle_reviewed_all` | — | `review_flow.handle_toggle_reviewed_all` | — | — |
| `refresh_diff` | — | `review_flow.handle_refresh_diff` | yes | yes |
| `reply` | `text` \| `audio_base64`, `marked_lines?` | `narration.handle_reply` | yes | yes |
| `request_change` | `text` \| `audio_base64`, `marked_lines?`, `severity?` | `comments.handle_request_change` | yes | yes |
| `edit_change_request` | `id`, `instruction`, `severity?` | `comments.handle_edit_change_request` | — | — |
| `remove_review_comment` | `id` | `comments.handle_remove_review_comment` | — | — |
| `finish_review` | `note?`, `as_skill?` | `comments.handle_finish_review` | — | — |
| `act_now` | `text` \| `audio_base64`, `marked_lines?` | `act_now.handle_act_now` | yes | yes |
| `refine_act_now` | `text` | `act_now.handle_refine_act_now` | yes | yes |
| `confirm_act_now` | — | `act_now.handle_confirm_act_now` | — | — |
| `step_into` | `text` | `explore.handle_step_into` | — | — |
| `list_all_files` | — | `explore.handle_list_all_files` | — | — |
| `explore_file` | `file_path` | `explore.handle_explore_file` | — | — |
| `explore_reply` | `text` \| `audio_base64`, `file_path` | `explore.handle_explore_reply` | yes | yes |
| `look_deeper` | `index`, `question?` | `research.handle_look_deeper` | yes | yes |
| `get_settings` | `provider?` | `settings.handle_get_settings` | — | — |
| `set_settings` | `settings` | `settings.handle_set_settings` | — | — |
| `set_voice_prefs` | `stt_enabled?`, `tts_enabled?` | `voice.handle_set_voice_prefs` | — | — |
| `open_md_preview` | `file_path` | `voice.handle_open_md_preview` | — | — |
| `speak_file` | `file_path`, `start_line?`, `end_line?` | `voice.handle_speak_file` | yes | yes |
| `speak_turn` | `text` | `voice.handle_speak_turn` | yes | yes |
| `explain_hunk` | `index` | `narration.handle_explain_hunk` | yes | yes |
| `set_narration_prefs` | `auto_narrate` | `narration.handle_set_narration_prefs` | — | — |
| `speak_text` | `text` | `voice.handle_speak_text` | yes | yes |

`stop` is the wire name for the button the UI labels **Interrupt**.

## Python → Browser

| Message | Sent by |
|---|---|
| `presenting` | `handlers/narration.py`, `handlers/review_flow.py`, `web/progress.py` |
| `narration` | `handlers/narration.py` |
| `human_turn` | `handlers/narration.py`, `handlers/explore.py` |
| `reviewer_turn` | `handlers/narration.py`, `handlers/explore.py` |
| `deeper_turn` | `handlers/research.py` |
| `audio_chunk` | `web/speech.py` (`try_speak`'s default `msg_type`) |
| `turn_audio_chunk` | `handlers/voice.py` via `try_speak` (a turn's speaker button) |
| `tour_audio_chunk` | `handlers/voice.py` via `try_speak` |
| `file_audio_chunk` | `handlers/voice.py` |
| `md_preview` | `handlers/voice.py` (`file_path`, `content_hash`, `blocks`, and `text`, the raw markdown) |
| `all_files` | `handlers/explore.py` |
| `file_explore` | `handlers/explore.py` |
| `definition` | `handlers/explore.py` |
| `review_progress` | `web/progress.py` |
| `review_comments_sync` | `app/server.py` (connect-time hydration) |
| `review_comment_queued` | `handlers/comments.py` |
| `review_comment_updated` | `handlers/comments.py` |
| `review_comment_removed` | `handlers/comments.py` |
| `review_finished` | `handlers/comments.py` (`plan_path`; `plan_file`, the repo-relative path the client opens with `open_md_preview`; `as_skill`, `skill_written`, `skill_note`; `instruction_line`, `/apply-review` when the skill was written; `comment_count`) |
| `act_now_preview` | `handlers/act_now.py` |
| `act_now_cleared` | `handlers/act_now.py` |
| `agent_stopped` | `handlers/research.py`, `handlers/act_now.py` via `web/runtime.py`'s `send_agent_stopped` (a Look deeper, Act Now or refine run cancelled before it answered; `kind` says which) |
| `settings` | `handlers/settings.py` |
| `context_too_large` | `handlers/narration.py`, `handlers/explore.py` |
| `service_status` | many — any handler that learns a service is up or down |
| `notice` | many — non-fatal, informational |
| `error` | `web/runtime.py` |

## Fields shared across messages

| Field | On | Defined in |
|---|---|---|
| `marked_lines` | `reply`, `request_change`, `act_now` | [app/web/context.py](../app/web/context.py) module docstring |
| `anchor` | `review_comment_queued`, `review_comments_sync` | `web/context.py`'s `line_context` |
| `content_hash` | `md_preview`, `file_audio_chunk` | [app/handlers/voice.py](../app/handlers/voice.py) module docstring |
| `chunk_index` / `chunk_count` | `audio_chunk`, `turn_audio_chunk`, `tour_audio_chunk`, `file_audio_chunk` | `web/speech.py`'s `try_speak` |
| `weight` / `partial` | `file_audio_chunk` blocks | `utils/markdown_speech.py`'s `ChunkBlock` |
| `sentences` (`text`, `weight`) | `audio_chunk`, `turn_audio_chunk`, `tour_audio_chunk` | `utils/speech_text.py`'s `sentence_segments` — the chat read-along highlight |
| `spoken` | `narration`, `reviewer_turn`, `deeper_turn` | the sanitised text a turn's speaker button sends back as `speak_turn` |
