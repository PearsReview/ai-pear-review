"""Shapes text on its way to the TTS endpoint. Two halves:

1. humanize_for_speech — WHAT the text says. Rewrites code-identifier- and
   filename-shaped text into speakable words.
2. split_for_speech — HOW MUCH goes in one request. The endpoint is a
   single-shot call with no streaming and no confirmed maximum length (see
   voice_service.py's docstring), so anything over the configured budget is
   split into pieces the caller sends as consecutive clips.

Both halves are TTS-only — neither ever touches what's stored in the
transcript or sent to the browser as text.

Kokoro, like most TTS, reads snake_case and camelCase literally, spells out
runs of capitals, and mishandles a literal "." inside a filename such as
`config.yaml`. None of that can be fixed by asking the model to phrase
things naturally, because it does not do so reliably — llama3.2:3b has been
observed mechanically narrating a diff despite the system prompt telling it
not to.

Heuristic, not a real parser: acronym-prefixed camelCase like "APIKey" or
"HTTPServer" won't split perfectly (only handles lowercase/digit ->
uppercase transitions, not upper-run -> capitalized-word) — good enough
for typical identifiers, same "heuristic, not ground truth" tradeoff as
code_search.py's definition patterns.

Note the two halves compose in one direction only: the size budget is
measured on the text as written, BEFORE humanization. Humanizing expands
text (`config.yaml` -> "config dot yaml"), so a piece sized at 800 can
reach the endpoint a few percent longer. That's deliberate — measuring
after humanization would make the *spoken* text the thing chunked, which
is what markdown_speech.py's per-block weights and the read-along
highlight are aligned against. Callers split first, then humanize each
piece; config.yaml's comment tells the user to leave headroom.
"""

from __future__ import annotations

import re

_IDENTIFIER_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
# A "." only counts as a filename/version/IP-shaped separator when it's
# hugged by word characters on both sides with no space — a real
# sentence-ending period is always followed by a space, so this never
# fires on ordinary prose.
_DOTTED_TOKEN_RE = re.compile(r"\b\w+(?:\.\w+)+\b")


def _looks_like_identifier(token: str) -> bool:
    return "_" in token or bool(re.search(r"[a-z0-9][A-Z]", token))


def _humanize_token(token: str) -> str:
    spaced = _CAMEL_BOUNDARY_RE.sub(" ", token.replace("_", " "))
    return re.sub(r"\s+", " ", spaced).strip().lower()


def _despeck_dots(text: str) -> str:
    """`config.yaml` -> "config dot yaml"; `run.py` -> "run dot py";
    `127.0.0.1` -> "127 dot 0 dot 0 dot 1"."""
    return _DOTTED_TOKEN_RE.sub(lambda m: m.group(0).replace(".", " dot "), text)


def humanize_for_speech(text: str) -> str:
    """`get_user_profile` -> "get user profile"; `myVariableName` -> "my
    variable name"; `MAX_RETRY_COUNT` -> "max retry count";
    `config.yaml` -> "config dot yaml". Plain words (`OK`, `I`, `the`, a
    real end-of-sentence period, ...) pass through unchanged."""
    text = _despeck_dots(text)

    def replace(match: re.Match[str]) -> str:
        token = match.group(0)
        return _humanize_token(token) if _looks_like_identifier(token) else token

    return _IDENTIFIER_TOKEN_RE.sub(replace, text)


# --- sizing for the TTS endpoint -----------------------------------------

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_WORD_RE = re.compile(r"\S+")


def _limit(value: int | None) -> int | None:
    """Normalizes "no limit" to a single representation. An omitted key
    gives None, a user writing `max_words: 0` plainly means "off", and a
    negative would otherwise reach _hard_split and loop forever — so all
    three collapse to None here, once, rather than each consumer guessing."""
    if not value or value < 0:
        return None
    return value


def word_count(text: str) -> int:
    """Counts words the same way _hard_split finds boundaries.

    str.split() with no argument, so runs of whitespace collapse and empty
    strings drop out."""
    return len(text.split())


def fits(text: str, max_chars: int | None = None, max_words: int | None = None) -> bool:
    """Whether `text` is small enough to send in one TTS request.

    The single home of the "whichever limit is tighter wins" rule. It falls
    out of the `and` — the packer stops as soon as *either* budget is
    exceeded, without ever comparing the two to each other, which would be
    wrong: 100 chars versus 20 words is the tighter constraint or the
    looser one depending entirely on the prose."""
    chars = _limit(max_chars)
    words = _limit(max_words)
    if chars is not None and len(text) > chars:
        return False
    # SIM103 would collapse these last three lines into a negated return.
    # Left as is: the two limits are checked in identical shape, and making
    # the second one structurally different from the first to save a line
    # costs more in readability than it saves.
    if words is not None and word_count(text) > words:  # noqa: SIM103
        return False
    return True


def _pack(pieces: list[str], joiner: str, max_chars: int | None, max_words: int | None) -> list[str]:
    """Greedily packs pieces into groups that each fit the budget. A single
    piece that's already over budget is emitted on its own — the caller is
    responsible for splitting those further."""
    packed: list[str] = []
    current = ""
    for piece in pieces:
        if not current:
            current = piece
        elif fits(current + joiner + piece, max_chars, max_words):
            current += joiner + piece
        else:
            packed.append(current)
            current = piece
    if current:
        packed.append(current)
    return packed


def _hard_split(text: str, max_chars: int | None, max_words: int | None) -> list[str]:
    """Last-resort cut for a piece with no sentence boundary left to use.

    Takes the tighter of two candidate offsets per pass — the last space at
    or before max_chars, and the end of the max_words'th word — and slices
    there. Deliberately slices by OFFSET and never rebuilds the text from
    `" ".join(text.split())`: rejoining would silently normalize internal
    whitespace, changing the text actually spoken and breaking
    markdown_speech.py's guarantee that a block's spoken text is exactly
    what its rendered spans say.

    Termination: every pass consumes at least one character. The char path
    falls back to a raw mid-word slice when a single word is longer than
    the whole budget (the only way to make progress on it); the word path's
    offset is the end of at least one non-space run, so it's >= 1 whenever
    a word limit is active at all — which _limit guarantees, since 0 and
    negatives became None. The max(cut, 1) is belt-and-braces: if it ever
    fires the alternative is an infinite loop emitting empty strings."""
    pieces: list[str] = []
    while not fits(text, max_chars, max_words):
        cut: int | None = None
        if max_chars is not None:
            at = text.rfind(" ", 0, max_chars)
            cut = max_chars if at <= 0 else at
        if max_words is not None:
            ends = [m.end() for m in _WORD_RE.finditer(text)][:max_words]
            # Fewer words present than the cap means the word budget isn't
            # what's failing — leave the cut to the char path.
            if len(ends) == max_words:
                cut = ends[-1] if cut is None else min(cut, ends[-1])
        if cut is None:
            break  # unreachable: not fits() implies some limit is active
        cut = max(cut, 1)
        pieces.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        pieces.append(text)
    return [p for p in pieces if p]


def _split_oversized(text: str, max_chars: int | None, max_words: int | None) -> list[str]:
    """Sentence boundaries first, word/character boundaries as the
    fallback — the descent used for any single piece too big to send."""
    sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.split(text) if s.strip()]
    out: list[str] = []
    for packed in _pack(sentences, " ", max_chars, max_words):
        if fits(packed, max_chars, max_words):
            out.append(packed)
        else:
            out.extend(_hard_split(packed, max_chars, max_words))
    return out


def split_for_speech(text: str, max_chars: int | None = None, max_words: int | None = None) -> list[str]:
    """Splits `text` into pieces that each fit one TTS request, preferring
    sentence boundaries. Returns [] for blank input, so a caller can treat
    that as "nothing to say" rather than synthesizing silence.

    Text already within budget is returned unchanged, as a single piece.
    That fast path isn't just an optimization: _split_oversized strips and
    re-joins on sentence boundaries, so without it, configuring no limit at
    all would still subtly rewrite every string sent to the endpoint."""
    chars = _limit(max_chars)
    words = _limit(max_words)
    if not text.strip():
        return []
    if fits(text, chars, words):
        return [text]
    return _split_oversized(text, chars, words)


def sentence_segments(text: str) -> list[dict[str, str | int]]:
    """The sentences of one TTS piece, each with its share of the clip's
    spoken length — what the chat's read-along highlight interpolates
    playback position across (see static/js/audio.js). Weighed on the
    HUMANIZED text, for the same reason markdown_speech._weight is: that's
    what was actually synthesized, so it's what the audio's length tracks.
    The text itself stays as written, because the frontend finds it in the
    rendered reply, which was never humanized."""
    sentences = (s.strip() for s in _SENTENCE_SPLIT_RE.split(text))
    return [{"text": s, "weight": len(humanize_for_speech(s))} for s in sentences if s]
