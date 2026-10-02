"""Shared JSON extraction for model replies that are supposed to be a
single JSON object, but may arrive wrapped in a markdown fence, or with
leading prose or trailing commentary despite instructions not to.

Decodes with json.JSONDecoder().raw_decode from the first "{", rather than
matching a greedy regex from the first "{" to the last "}". A greedy span
turns two JSON objects in one reply — or one object followed by any
trailing text containing "}" — into garbage instead of the first valid
object. raw_decode stops as soon as one complete, valid object is parsed.
"""

from __future__ import annotations

import json


def extract_json_object(text: str) -> dict:
    """Finds and parses the first complete JSON object in text, tolerating
    a leading markdown fence or prose before it. Raises json.JSONDecodeError
    (letting callers handle it the same way a direct json.loads() failure
    would) if no valid object can be found."""
    start = text.find("{")
    if start == -1:
        raise json.JSONDecodeError("no '{' found in text", text, 0)
    decoder = json.JSONDecoder()
    obj, _end = decoder.raw_decode(text, start)
    if not isinstance(obj, dict):
        raise json.JSONDecodeError("parsed value was not a JSON object", text, start)
    return obj
