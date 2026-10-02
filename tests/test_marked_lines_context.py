"""Unit tests for app/web/context.py's marked_lines_context — the prompt block
built from a reviewer's marked-line selection for conversational replies
(handle_reply). Distinct from line_context (request_change/act_now),
which intentionally keeps snippets as plain text — see that function's
docstring and callers for why.

The property under test: a selection spanning a removed line and its
replacement added line must not read, to the LLM, like two lines of
final source both present at once (the "duplicate key" failure mode) —
it must carry real +/- diff markers and say outright that it's a diff
excerpt.
"""

from app.web.context import marked_lines_context


def _line(kind, old_lineno, new_lineno, text):
    return {
        "file_path": "app/config.yaml",
        "old_lineno": old_lineno,
        "new_lineno": new_lineno,
        "text": text,
        "kind": kind,
    }


def test_add_and_del_lines_get_diff_markers():
    marked_lines = [
        _line("del", 5, None, "    model: llama3.2:3b-instruct-q4_K_M"),
        _line("add", None, 5, "    model: qwen2.5-coder:7b-instruct-q4_K_M"),
    ]
    result = marked_lines_context(marked_lines)
    assert "-    model: llama3.2:3b-instruct-q4_K_M" in result
    assert "+    model: qwen2.5-coder:7b-instruct-q4_K_M" in result
    # Bare (unprefixed) duplicate lines are exactly the misreading this fixes.
    assert "\n    model: llama3.2:3b-instruct-q4_K_M\n" not in result


def test_explains_diff_semantics():
    marked_lines = [_line("add", None, 5, "    model: qwen2.5-coder:7b-instruct-q4_K_M")]
    result = marked_lines_context(marked_lines)
    assert "diff excerpt" in result
    assert "were added" in result and "were removed" in result


def test_context_lines_stay_unprefixed():
    marked_lines = [_line("context", 1, 1, "ollama:")]
    result = marked_lines_context(marked_lines)
    assert " ollama:" in result or "\nollama:" in result
    assert "+ollama:" not in result and "-ollama:" not in result


def test_missing_kind_falls_back_to_context_for_old_payloads():
    # A payload from before "kind" was threaded through (see buildMarkedContext
    # in static/js/interactions.js) has no "kind" key at all — must degrade to today's
    # unprefixed behavior rather than KeyError or mis-marking as add/del.
    marked_lines = [{"file_path": "x.py", "old_lineno": 1, "new_lineno": 1, "text": "pass"}]
    result = marked_lines_context(marked_lines)
    assert "+pass" not in result and "-pass" not in result
    assert "pass" in result


def test_no_marked_lines_returns_none():
    assert marked_lines_context(None) is None
    assert marked_lines_context([]) is None
