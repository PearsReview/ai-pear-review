"""extract_json_object (app/utils/json_utils.py) had zero test coverage
despite being load-bearing on the Anthropic path: it's the fallback
parser briefing_service.py falls back to
there, since generate_once's response_schema (grammar-constrained
decoding) is Ollama-only — see conversation_service.py's own docstring,
and README.md's Act Now section, which now says so explicitly rather
than the "or the equivalent on Anthropic" claim it used to make.
"""

from __future__ import annotations

import json

import pytest

from app.utils.json_utils import extract_json_object


def test_extracts_a_bare_json_object():
    assert extract_json_object('{"intent": "fixes a bug"}') == {"intent": "fixes a bug"}


def test_strips_a_leading_markdown_fence():
    text = '```json\n{"intent": "fixes a bug"}\n```'
    assert extract_json_object(text) == {"intent": "fixes a bug"}


def test_strips_leading_prose():
    text = 'Sure, here is the JSON you asked for:\n{"intent": "fixes a bug"}'
    assert extract_json_object(text) == {"intent": "fixes a bug"}


def test_strips_trailing_commentary():
    text = '{"intent": "fixes a bug"}\n\nLet me know if you need anything else!'
    assert extract_json_object(text) == {"intent": "fixes a bug"}


def test_picks_the_first_object_when_the_reply_is_followed_by_more_brace_text():
    """The correctness fix the module docstring calls out: a greedy
    first-'{'-to-last-'}' regex would swallow the trailing text below (it
    contains a brace) into one broken parse. raw_decode from the first
    '{' must stop as soon as the FIRST object is complete."""
    text = '{"intent": "fixes a bug"} (see also {"unrelated": "aside"})'
    assert extract_json_object(text) == {"intent": "fixes a bug"}


def test_handles_nested_objects_and_arrays():
    text = '{"intent": "fixes a bug", "steps": [{"n": 1}, {"n": 2}], "meta": {"ok": true}}'
    assert extract_json_object(text) == {
        "intent": "fixes a bug",
        "steps": [{"n": 1}, {"n": 2}],
        "meta": {"ok": True},
    }


def test_raises_json_decode_error_when_no_brace_is_present():
    with pytest.raises(json.JSONDecodeError):
        extract_json_object("no JSON here at all")


def test_raises_json_decode_error_on_malformed_json_after_the_brace():
    with pytest.raises(json.JSONDecodeError):
        extract_json_object('{"intent": "unterminated')


def test_skips_a_leading_array_and_finds_the_object_nested_inside_it():
    """text.find("{") lands on the object nested inside the array, not the
    array's own '[' — so this returns the object rather than raising.
    Pinned because it's easy to assume the "not a dict" guard in
    extract_json_object exists to catch a leading array; it can't, since
    raw_decode starting exactly at a '{' can only ever produce a dict or
    fail outright. That guard is defensive against a future change to how
    `start` is located, not reachable via find("{") today."""
    text = 'note: [{"intent": "fixes a bug"}]'
    assert extract_json_object(text) == {"intent": "fixes a bug"}
