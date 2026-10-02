"""Parses a raw .md file into line-anchored blocks, then packs those blocks
into TTS-sized chunks (see "speak_file" and "open_md_preview" in app/handlers/voice.py).
Also sanitises the persona's own replies (narration, chat, explore) before
they reach the transcript or TTS — see sanitize_persona_reply below.

Two stages, in this order (see prepare_file_for_speech):

1. parse_markdown_blocks — a line-oriented state machine turning the source
   into Blocks, each of which knows the source line range it came from and
   carries BOTH its spoken form (cleaned for TTS) and its render form (a
   list of inline Spans the frontend turns into real DOM elements).
2. chunk_blocks — packs consecutive blocks' spoken text into chunks under a
   character budget, since the TTS endpoint is a single-shot call with no
   streaming and no confirmed max length (see voice_service.py's docstring)
   — a whole file is almost always too long for one call.

parse_markdown_blocks does double duty on the persona's own replies. The
persona prompt says "reply with only the spoken words — no labels, no
markdown, no lists", and is measured (qa_agent/generated/'s narration pack)
to ignore that roughly one time in five, either announcing itself ("Sure,
I'll present this hunk.") or re-pasting the diff it was shown in a fence.
Asking harder doesn't fix it reliably, and the failure shape differs by
model. Markdown is the one delimiter every model emits unprompted, so
sanitize_persona_reply parses a reply the same way a file is parsed and
drops the two shapes actually recorded, rather than adding a second parser
or a bespoke marker.

Still heuristic, not a real CommonMark parser — the same "not ground truth,
good enough" tradeoff speech_text.py takes for identifiers.

Line-oriented rather than whole-document regex substitution, for two
reasons. Substitution destroys line provenance, and everything the preview
does — render a block, let the reviewer select one, highlight the one being
spoken — needs to know which source lines a piece of speech came from.

It also removes a bug class structurally: a MULTILINE ^...$ pattern with a
leading or trailing \\s* will happily eat through a blank line into
the next block, since a blank line is itself just whitespace, silently
merging two paragraphs meant to stay separate. Blocks here are delimited by
line index, so no regex can reach past the lines it was handed.

The core invariant, relied on by the highlight feature: a block's spoken
text is DERIVED from its spans, never computed alongside them.

    block.spoken_text == _ensure_pause(_clean_spoken("".join(s.text for s in spans)))

So what the preview shows and what the voice says agree by construction.
Two independent pipelines would drift silently and make the highlight a
lie. The one deliberate exception is strip_unreadable_chars, which applies
to spoken_text only: emoji should be visible in the preview and inaudible
in the audio.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .speech_text import fits, humanize_for_speech, split_for_speech

# --- inline (within one block) ------------------------------------------

# One alternation scanned with finditer, rather than sequential re.sub
# passes. Sequential substitution cannot produce spans: by the time the
# second pass runs, the first pass's positions are gone. The alternatives
# are listed in precedence order (image before link, bold before italic,
# and so on), and Python's alternation is leftmost-first, so at any given
# position the intended alternative wins.
#
# Asterisk bold/italic markers never collide with identifiers, so no
# boundary guard is needed. Underscore markers do: snake_case identifiers
# (get_user_profile) and dunder names (__init__) are built from the same
# character markdown uses for emphasis, so those variants additionally
# require a non-word character (or string edge) outside each marker —
# that's what stops "get_user_profile" being read as "get" +
# italic("user") + "profile". See _span_for_match for the extra dunder
# guard that boundary alone can't provide.
_INLINE_SPAN_RE = re.compile(
    r"(?P<image>!\[(?P<image_text>[^\]]*)\]\((?P<image_href>[^)]*)\))"
    r"|(?P<link>\[(?P<link_text>[^\]]*)\]\((?P<link_href>[^)]*)\))"
    r"|(?P<code>`(?P<code_text>[^`]*)`)"
    r"|(?P<bold_a>\*\*(?P<bold_a_text>.+?)\*\*)"
    r"|(?P<bold_u>(?<!\w)__(?!_)(?P<bold_u_text>.+?)(?<!_)__(?!\w))"
    r"|(?P<italic_a>(?<!\*)\*(?!\*)(?P<italic_a_text>.+?)(?<!\*)\*(?!\*))"
    r"|(?P<italic_u>(?<!\w)_(?!_)(?P<italic_u_text>.+?)(?<!_)_(?!\w))"
    r"|(?P<strike>~~(?P<strike_text>.+?)~~)"
)

_DUNDER_RE = re.compile(r"\w+")

# --- line classifiers ----------------------------------------------------

# These are the same patterns the old substitution passes used, but applied
# with .match() to one line at a time instead of .sub() across the whole
# document — which is what makes them incapable of reaching past their line.
_FENCE_RE = re.compile(r"^[ \t]*(?P<ticks>`{3,})[ \t]*(?P<lang>[^`]*)$")
_HEADING_RE = re.compile(r"^(?P<hashes>#{1,6})[ \t]+(?P<text>.*)$")
_TABLE_ROW_RE = re.compile(r"^[ \t]*\|(?P<body>.+)\|[ \t]*$")
_HR_RE = re.compile(r"^(?:-{3,}|\*{3,}|_{3,})[ \t]*$")
_QUOTE_RE = re.compile(r"^[ \t]*>[ \t]?(?P<text>.*)$")
_LIST_ITEM_RE = re.compile(r"^(?P<indent>[ \t]*)(?P<marker>[-*+]|\d+\.)[ \t]+(?P<text>.*)$")
# A table cell that's only dashes/colons/spaces — i.e. the |---|:--:| row
# that defines column alignment. Structural only, never spoken.
_SEPARATOR_CELL_RE = re.compile(r"^[:\- ]+$")

_BLANK_RUN_RE = re.compile(r"\n{3,}")
_SPACE_RUN_RE = re.compile(r"[ \t]{2,}")
_ENDS_WITH_PAUSE_RE = re.compile(r"[.!?:;,]\s*$")

CODE_BLOCK_SPOKEN = "(code block omitted)"

# --- types ---------------------------------------------------------------


@dataclass(frozen=True)
class Span:
    """One run of inline text plus how it should be rendered. The frontend
    turns each span into a real DOM node with .textContent — it never
    parses or injects markup, which is why this is a structured list rather
    than an HTML string."""

    text: str
    style: str = "plain"  # plain | bold | italic | code | link | strike
    # link only. Rendered as a title tooltip, never an <a href> — a
    # file-derived href would be a javascript:-URI injection surface this
    # app doesn't otherwise have anywhere.
    href: str = ""


@dataclass
class Block:
    """One structural unit of the document, anchored to the source lines it
    came from. spoken_text == "" means "never spoken" (a horizontal rule),
    which also means never chunked and never highlighted."""

    kind: str  # heading | paragraph | list_item | quote | code | table_row | rule
    start_line: int  # 1-based, inclusive, into the ORIGINAL source
    end_line: int  # 1-based, inclusive
    spoken_text: str = ""
    spans: list[Span] = field(default_factory=list)
    level: int = 0  # heading level 1-6; list nesting depth; else 0
    ordered: bool = False  # list_item
    marker: str = ""  # list_item — the author's literal "3.", so ordered lists keep their own numbering
    code_text: str = ""  # code — fence lines excluded
    code_lang: str = ""  # code — the info string
    cells: list[str] = field(default_factory=list)  # table_row
    is_header: bool = False  # table_row — this row had a separator under it
    table_group: int = -1  # table_row — consecutive rows sharing one table


@dataclass
class ChunkBlock:
    """One block's participation in one chunk. `weight` is that slice's
    share of the chunk's spoken length, measured on the *humanized* text
    (see speech_text.py) because that is what actually gets synthesized.
    The frontend interpolates audioEl.currentTime/duration across these
    weights to work out which block is being spoken right now — as
    fine-grained as this can honestly get, given the TTS endpoint returns
    one opaque audio blob per chunk with no word timings. The estimate
    drifts within a chunk and re-syncs exactly at every chunk boundary; a
    client with no preview open ignores all of it. `partial` marks a block
    big enough that it had to be split across several chunks."""

    block_index: int
    start_line: int
    end_line: int
    weight: int
    partial: bool = False


@dataclass
class Chunk:
    text: str
    start_line: int
    end_line: int
    blocks: list[ChunkBlock] = field(default_factory=list)


# --- inline parsing ------------------------------------------------------


def _span_for_match(match: re.Match[str]) -> Span:
    if match.group("image") is not None:
        return Span(match.group("image_text"), "plain")
    if match.group("link") is not None:
        return Span(match.group("link_text"), "link", match.group("link_href"))
    if match.group("code") is not None:
        return Span(match.group("code_text"), "code")
    if match.group("bold_a") is not None:
        return Span(match.group("bold_a_text"), "bold")
    if match.group("bold_u") is not None:
        inner = match.group("bold_u_text")
        # Word-boundary guards stop "get_user_profile" being read as
        # italic("user"), but they can't catch a dunder like "__init__" or
        # "__main__": there the whole token IS flanked by real word
        # boundaries, which is indistinguishable from genuine __bold__ by
        # boundary alone. A single \w+ run with no internal whitespace is
        # far more likely to be a dunder than intentional emphasis, so it
        # stays literal — in the preview and in the audio alike. Applied
        # only to the double-underscore form, where the collision is common
        # (dunders are everywhere in Python docs) and the cost of also
        # skipping a genuine one-word __bold__ is low; the single-underscore
        # italic form is left unguarded since one-word _italic_ is common
        # prose and single-underscore-wrapped identifiers are rare.
        if _DUNDER_RE.fullmatch(inner):
            return Span(match.group(0), "plain")
        return Span(inner, "bold")
    if match.group("italic_a") is not None:
        return Span(match.group("italic_a_text"), "italic")
    if match.group("italic_u") is not None:
        return Span(match.group("italic_u_text"), "italic")
    return Span(match.group("strike_text"), "strike")


def _inline_spans(raw: str) -> list[Span]:
    """Splits one block's raw text into styled spans, with the gaps between
    matches becoming plain spans. Adjacent plain spans are merged so a
    dunder left literal above doesn't fragment the surrounding prose."""
    spans: list[Span] = []
    pos = 0

    def add(span: Span) -> None:
        if not span.text:
            return
        if spans and span.style == "plain" and spans[-1].style == "plain":
            spans[-1] = Span(spans[-1].text + span.text, "plain")
            return
        spans.append(span)

    for match in _INLINE_SPAN_RE.finditer(raw):
        add(Span(raw[pos : match.start()], "plain"))
        add(_span_for_match(match))
        pos = match.end()
    add(Span(raw[pos:], "plain"))
    return spans


# --- speech text ---------------------------------------------------------

# Denylist, not an allowlist: strip only known-bad ranges (control
# characters, emoji/pictograph/symbol blocks) so real prose in any
# language — accented Latin, Cyrillic, Greek, CJK, etc. — is never touched.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_UNREADABLE_RANGES_RE = re.compile(
    "["
    "\U0001f1e6-\U0001f1ff"  # regional indicator letters (flag emoji)
    "\U0001f300-\U0001faff"  # misc symbols/pictographs, emoticons, transport, supplemental symbols
    "\U00002600-\U000027bf"  # misc symbols, dingbats
    "\U0000fe00-\U0000fe0f"  # variation selectors
    "\U0000200d"  # zero-width joiner (emoji composition)
    "]"
)


def strip_unreadable_chars(text: str) -> str:
    """Removes control characters and emoji/pictograph/symbol codepoints
    that a TTS voice can't meaningfully pronounce, then collapses the
    whitespace runs those removals tend to leave behind. Deliberately a
    denylist of specific bad ranges rather than an allowlist of "good"
    ones — an allowlist would have to enumerate every script a reviewer
    might write a doc in.

    Applied to spoken text only, never to spans: emoji belong in the
    preview, just not in the audio."""
    text = _CONTROL_CHARS_RE.sub("", text)
    text = _UNREADABLE_RANGES_RE.sub("", text)
    text = _SPACE_RUN_RE.sub(" ", text)
    text = _BLANK_RUN_RE.sub("\n\n", text)
    return text


def _ensure_pause(line: str) -> str:
    """Appends a "." if `line` doesn't already end in something that reads
    as a pause (./!/?/:/;/,) — the one mechanism this module has for
    "clearly defined pauses" at structural boundaries (headings, list
    items, quotes, table rows) that raw markdown expresses with a marker or
    a newline, neither of which survives to the TTS model. A no-op on an
    empty line (a blank bullet or cell — nothing to punctuate)."""
    line = line.rstrip()
    if not line or _ENDS_WITH_PAUSE_RE.search(line):
        return line
    return line + "."


def _spoken_from_spans(spans: list[Span], *, pause: bool) -> str:
    """The module's core invariant lives here: spoken text is derived from
    the spans the preview renders, so the two can't disagree. `pause` is
    True for the block kinds whose separation from the next block is
    structural rather than punctuational (headings, list items, quotes,
    table rows) — a paragraph already ends in its own punctuation."""
    text = strip_unreadable_chars("".join(s.text for s in spans))
    text = _SPACE_RUN_RE.sub(" ", text).strip()
    return _ensure_pause(text) if pause else text


# --- block parsing -------------------------------------------------------


def _is_closing_fence(line: str) -> bool:
    """A bare ``` with no info string. An opening fence may carry a
    language (```python); a closing one never does, which is what lets one
    pattern serve both roles."""
    match = _FENCE_RE.match(line)
    return bool(match) and not match.group("lang").strip()


def _is_separator_row(line: str) -> bool:
    match = _TABLE_ROW_RE.match(line)
    if not match:
        return False
    cells = [c.strip() for c in match.group("body").split("|")]
    cells = [c for c in cells if c]
    return bool(cells) and all(_SEPARATOR_CELL_RE.match(c) for c in cells)


def _starts_new_block(line: str) -> bool:
    """Whether `line` begins a block of its own, i.e. ends whatever
    paragraph or list item is currently being accumulated."""
    if not line.strip():
        return True
    return bool(
        _FENCE_RE.match(line)
        or _HEADING_RE.match(line)
        or _TABLE_ROW_RE.match(line)
        or _HR_RE.match(line)
        or _QUOTE_RE.match(line)
        or _LIST_ITEM_RE.match(line)
    )


def parse_markdown_blocks(text: str) -> list[Block]:
    """Turns raw markdown into line-anchored blocks. Classification order
    below is load-bearing — in particular a table separator row is only
    recognised immediately after a table row, which is what makes a
    standalone "---" unambiguously a horizontal rule rather than the two
    patterns fighting over it the way the old substitution passes did."""
    lines = text.split("\n")
    total = len(lines)
    blocks: list[Block] = []
    table_group = 0
    prev_was_table_row = False
    i = 0

    while i < total:
        raw = lines[i]

        if not raw.strip():
            i += 1
            prev_was_table_row = False
            continue

        fence = _FENCE_RE.match(raw)
        if fence:
            start = i
            lang = fence.group("lang").strip()
            i += 1
            code_lines: list[str] = []
            while i < total and not _is_closing_fence(lines[i]):
                code_lines.append(lines[i])
                i += 1
            if i < total:
                end = i  # the closing fence line itself
                i += 1
            else:
                # An unclosed fence runs to EOF rather than hanging. Trailing
                # blank lines are given back rather than claimed: text ending
                # in "\n" splits to a final empty element, and letting the
                # fence own it would report an end_line past the last line
                # anyone actually wrote.
                while code_lines and not code_lines[-1].strip():
                    code_lines.pop()
                end = start + len(code_lines)
            blocks.append(
                Block(
                    kind="code",
                    start_line=start + 1,
                    end_line=end + 1,
                    code_text="\n".join(code_lines),
                    code_lang=lang,
                    # Kept speakable (rather than silent) deliberately: it
                    # preserves the pre-preview audio behavior, and it gives
                    # the reading highlight something to land on so the code
                    # box lights up while the voice says so, instead of
                    # being a dead zone the highlight jumps over.
                    spoken_text=_ensure_pause(CODE_BLOCK_SPOKEN),
                )
            )
            prev_was_table_row = False
            continue

        heading = _HEADING_RE.match(raw)
        if heading:
            spans = _inline_spans(heading.group("text"))
            blocks.append(
                Block(
                    kind="heading",
                    start_line=i + 1,
                    end_line=i + 1,
                    level=len(heading.group("hashes")),
                    spans=spans,
                    spoken_text=_spoken_from_spans(spans, pause=True),
                )
            )
            i += 1
            prev_was_table_row = False
            continue

        if _is_separator_row(raw):
            # Normally consumed by the header row's own lookahead below;
            # reaching here means a stray one (a table with no header row).
            # Structural either way — emits nothing.
            i += 1
            continue

        table = _TABLE_ROW_RE.match(raw)
        if table:
            if not prev_was_table_row:
                table_group += 1
            cells = [c.strip() for c in table.group("body").split("|")]
            cells = [c for c in cells if c]
            end = i
            is_header = False
            if i + 1 < total and _is_separator_row(lines[i + 1]):
                is_header = True
                end = i + 1
            spans = _inline_spans(", ".join(cells))
            blocks.append(
                Block(
                    kind="table_row",
                    start_line=i + 1,
                    end_line=end + 1,
                    cells=cells,
                    is_header=is_header,
                    table_group=table_group,
                    spans=spans,
                    spoken_text=_spoken_from_spans(spans, pause=True),
                )
            )
            i = end + 1
            prev_was_table_row = True
            continue

        if _HR_RE.match(raw):
            blocks.append(Block(kind="rule", start_line=i + 1, end_line=i + 1))
            i += 1
            prev_was_table_row = False
            continue

        quote = _QUOTE_RE.match(raw)
        if quote:
            # A blockquote is normally one continuous quote line-wrapped
            # across several "> " lines, not a sequence of separate items —
            # so the whole contiguous run becomes ONE block with one pause
            # at the end, rather than a pause per wrapped line chopping a
            # sentence into fragments.
            start = i
            parts: list[str] = []
            while i < total:
                match = _QUOTE_RE.match(lines[i])
                if not match:
                    break
                parts.append(match.group("text").strip())
                i += 1
            spans = _inline_spans(" ".join(p for p in parts if p))
            blocks.append(
                Block(
                    kind="quote",
                    start_line=start + 1,
                    end_line=i,
                    spans=spans,
                    spoken_text=_spoken_from_spans(spans, pause=True),
                )
            )
            prev_was_table_row = False
            continue

        item = _LIST_ITEM_RE.match(raw)
        if item:
            start = i
            parts = [item.group("text").strip()]
            i += 1
            # Fold continuation lines (a wrapped bullet) into the same item,
            # so a wrapped bullet selects and highlights as one unit.
            while i < total and not _starts_new_block(lines[i]):
                parts.append(lines[i].strip())
                i += 1
            marker = item.group("marker")
            spans = _inline_spans(" ".join(p for p in parts if p))
            blocks.append(
                Block(
                    kind="list_item",
                    start_line=start + 1,
                    end_line=i,
                    # Two spaces per nesting level — the common convention,
                    # and the frontend only uses this for indentation depth.
                    level=len(item.group("indent").expandtabs(4)) // 2,
                    ordered=marker.endswith("."),
                    marker=marker,
                    spans=spans,
                    spoken_text=_spoken_from_spans(spans, pause=True),
                )
            )
            prev_was_table_row = False
            continue

        start = i
        parts = [raw.strip()]
        i += 1
        while i < total and not _starts_new_block(lines[i]):
            parts.append(lines[i].strip())
            i += 1
        spans = _inline_spans(" ".join(p for p in parts if p))
        blocks.append(
            Block(
                kind="paragraph",
                start_line=start + 1,
                end_line=i,
                spans=spans,
                spoken_text=_spoken_from_spans(spans, pause=False),
            )
        )
        prev_was_table_row = False

    return blocks


# --- persona-reply sanitising ---------------------------------------------
#
# Two shapes of noise, both recorded verbatim in qa_agent/generated/'s
# narration pack, and nothing else:
#
#   "Sure, I'll present this hunk.\n\nThe change in shipment.py modifies..."
#   "Sure, here's the diff hunk for shipment.py:\n\n```diff\n@@ -37,4...\n```"
#
# The first is an announcement instead of content — dropped as a leading
# paragraph block. The second is the model re-pasting a diff the reviewer
# is already looking at in the code pane — dropped as a code block. Neither
# check touches anything else: inline `code`, bold, lists and a code block
# that's a genuinely new snippet all still render exactly as parsed.

# Kept in sync with qa_agent/scenarios/scenario_helpers.py's
# _STYLE_VIOLATION_PATTERNS canned-preamble / announces-the-act / opener /
# speaker-label rules by tests/test_style_violations.py, which runs both
# sets over the same recorded corpus and asserts they agree. Two intentional
# differences from that module's copy: no markdown-fence or bare-@@-header
# pattern here, because those already have a dedicated, more precise check
# below (_looks_like_diff_dump compares against the actual diff shown,
# rather than just matching the shape) — and these are matched against one
# already-parsed paragraph block, not scanned across the raw string, so a
# fence appearing later in a multi-paragraph reply can't false-positive a
# block that merely opens with "Sure,".
_ANNOUNCEMENT_PATTERNS = (
    re.compile(r"^\s*(?:sure|certainly|of course|okay|ok)\b[^.!?\n]*:", re.IGNORECASE),
    re.compile(
        r"^\s*(?:sure|certainly|of course|okay|ok)\b[,.]?\s*"
        r"(?:i(?:'|’)?ll|i\s+will|let(?:'|’)?s|let\s+me|i\s+can)\b",
        re.IGNORECASE,
    ),
    re.compile(r"^\s*here(?:'|’)?(?:s| is)\b", re.IGNORECASE),
    re.compile(r"^\s*(narration|assistant|reply|answer)\s*:", re.IGNORECASE),
)

_DIFF_HEADER_RE = re.compile(r"^\s*@@ -\d", re.MULTILINE)

# How much of a fenced block's non-blank lines have to appear verbatim in
# the diff shown before it counts as a re-paste rather than a new snippet.
# Not 1.0: the model sometimes reformats whitespace or drops the +/- diff
# markers while copying, and a block that's 90% the same code is still the
# same code, not a fresh example worth keeping on screen twice.
_DIFF_DUMP_LINE_OVERLAP = 0.7


def _is_announcement(text: str) -> bool:
    stripped = text.strip()
    return bool(stripped) and any(pattern.search(stripped) for pattern in _ANNOUNCEMENT_PATTERNS)


def _looks_like_diff_dump(code_text: str, code_lang: str, diff_context: str) -> bool:
    """Whether a fenced code block is redundant with the diff already on
    screen, rather than a genuinely new snippet worth showing.

    Three signals, cheapest and most certain first: the model labelled the
    fence itself (```diff / ```patch), the block contains a real diff hunk
    header (works even when the fence isn't labelled — the recorded example
    above had both), or most of its lines appear verbatim in the diff.
    """
    if code_lang.strip().lower() in ("diff", "patch"):
        return True
    if _DIFF_HEADER_RE.search(code_text):
        return True
    if not diff_context:
        return False
    lines = [line for line in code_text.splitlines() if line.strip()]
    if not lines:
        return False
    hits = sum(1 for line in lines if line.strip() in diff_context)
    return hits / len(lines) >= _DIFF_DUMP_LINE_OVERLAP


def sanitize_persona_reply(text: str, diff_context: str = "") -> tuple[list[Block], str]:
    """Parses one persona reply (narration, chat reply, or explore reply)
    and drops the two shapes of noise described above.

    Returns (blocks, spoken_text): `blocks` for the transcript to render
    (via block_to_payload), `spoken_text` for try_speak. The caller's own
    `text` — the full, original response — is left untouched: it's still
    what gets stored in transcript/history and sent as the WS message's own
    "text" field, since llm_capture.py's find_capture_for_response joins a
    capture file to a WS frame by exact response text, and changing what's
    sent there would silently break that join.

    Never strips to nothing. If dropping the announcement and/or a
    redundant code block would leave no spoken content at all, the
    original unfiltered blocks are returned instead — an empty reply is a
    worse failure than a noisy one, and this is the one case where showing
    the raw text beats "fixing" it.
    """
    blocks = parse_markdown_blocks(text)
    if not blocks:
        return blocks, text.strip()

    kept: list[Block] = []
    for index, block in enumerate(blocks):
        if index == 0 and block.kind == "paragraph" and _is_announcement(block.spoken_text):
            continue
        if block.kind == "code" and _looks_like_diff_dump(block.code_text, block.code_lang, diff_context):
            continue
        kept.append(block)

    fallback_spoken = "\n\n".join(b.spoken_text for b in blocks if b.spoken_text).strip() or text.strip()
    if not kept:
        return blocks, fallback_spoken

    spoken = "\n\n".join(b.spoken_text for b in kept if b.spoken_text).strip()
    if not spoken:
        return blocks, fallback_spoken
    return kept, spoken


# --- chunking ------------------------------------------------------------

# The generic "how much fits in one TTS request" machinery lives in
# speech_text.py — narration is plain text with nothing markdown about it,
# so it can't be made to import this module to be spoken. What stays here
# is the block-aware part: packing blocks while preserving the line
# provenance and per-block weights the read-along highlight needs.
_CHUNK_JOINER = "\n\n"


def _weight(text: str) -> int:
    """A slice's share of its chunk's spoken length, measured on the
    HUMANIZED text — handle_speak_file humanizes each chunk immediately before
    synthesis (config.yaml -> "config dot yaml"), so a weight taken from
    the raw text would describe audio that was never produced and the
    highlight would skew."""
    return len(humanize_for_speech(text))


def chunk_blocks(
    blocks: list[Block],
    max_chars: int | None,
    include: set[int] | None = None,
    *,
    max_words: int | None = None,
) -> list[Chunk]:
    """Packs blocks into chunks that fit one TTS request, preserving block
    provenance. The budget is whatever speech_text.fits() accepts —
    max_chars, max_words, or both, whichever is tighter; either may be None
    for "no limit of that kind". Blocks with no spoken text (horizontal
    rules) are skipped entirely — they're never spoken, so they're never
    chunked and never highlighted.

    `include`, when given, restricts chunking to those block indices (a
    reviewer's preview selection — see handle_speak_file). It is a filter
    applied *here* rather than by the caller slicing the list first, and
    that matters: block_index has to stay an index into the whole
    document's block list, because that's what the frontend's preview is
    keyed on. Slicing first would renumber from zero and the highlight
    would land on the wrong paragraphs.

    A block too big for one chunk is split across several, appearing in
    each with partial=True and only that slice's weight. The frontend keeps
    it highlighted throughout, which is what you want: a huge paragraph
    genuinely is still being read.

    Because blocks are joined with "\\n\\n" and neither identifier pattern
    in speech_text.py can match across a newline, per-block and whole-chunk
    humanization agree exactly:

        sum(cb.weight for cb in chunk.blocks)
            == len(humanize_for_speech(chunk.text)) - 2 * (len(chunk.blocks) - 1)

    That identity is what guarantees the weights describe the audio that is
    actually synthesized; it's asserted in the tests because nothing else
    would catch a silent desync if speech_text.py ever changed.

    Note the identity is partition-independent: it has no term referencing
    the budget. It states what ended up *in* a chunk, so it holds for any
    grouping of blocks — tightening max_chars, or adding a word limit, only
    moves where flush() happens, never what a flushed chunk contains."""
    chunks: list[Chunk] = []
    texts: list[str] = []
    members: list[ChunkBlock] = []

    def flush() -> None:
        nonlocal texts, members
        if members:
            chunks.append(
                Chunk(
                    text=_CHUNK_JOINER.join(texts),
                    start_line=min(m.start_line for m in members),
                    end_line=max(m.end_line for m in members),
                    blocks=list(members),
                )
            )
        texts = []
        members = []

    for index, block in enumerate(blocks):
        spoken = block.spoken_text
        if not spoken:
            continue
        if include is not None and index not in include:
            continue

        if not fits(spoken, max_chars, max_words):
            flush()
            for piece in split_for_speech(spoken, max_chars, max_words):
                chunks.append(
                    Chunk(
                        text=piece,
                        start_line=block.start_line,
                        end_line=block.end_line,
                        blocks=[
                            ChunkBlock(
                                block_index=index,
                                start_line=block.start_line,
                                end_line=block.end_line,
                                weight=_weight(piece),
                                partial=True,
                            )
                        ],
                    )
                )
            continue

        if texts and not fits(_CHUNK_JOINER.join(texts + [spoken]), max_chars, max_words):
            flush()
        texts.append(spoken)
        members.append(
            ChunkBlock(
                block_index=index,
                start_line=block.start_line,
                end_line=block.end_line,
                weight=_weight(spoken),
            )
        )

    flush()
    return chunks


# --- wire payloads -------------------------------------------------------
# Kept here rather than in app/handlers/ so the wire shape has one owner and
# the handlers stay free of field-by-field marshalling.


def span_to_payload(span: Span) -> dict:
    payload = {"text": span.text, "style": span.style}
    if span.href:
        payload["href"] = span.href
    return payload


def block_to_payload(block: Block) -> dict:
    payload = {
        "kind": block.kind,
        "start_line": block.start_line,
        "end_line": block.end_line,
        "level": block.level,
        "spans": [span_to_payload(s) for s in block.spans],
    }
    if block.kind == "code":
        payload["code_text"] = block.code_text
        payload["code_lang"] = block.code_lang
    elif block.kind == "list_item":
        payload["ordered"] = block.ordered
        payload["marker"] = block.marker
    elif block.kind == "table_row":
        payload["cells"] = block.cells
        payload["is_header"] = block.is_header
        payload["table_group"] = block.table_group
    return payload


def chunk_block_to_payload(member: ChunkBlock) -> dict:
    return {
        "block_index": member.block_index,
        "start_line": member.start_line,
        "end_line": member.end_line,
        "weight": member.weight,
        "partial": member.partial,
    }


# --- composition ---------------------------------------------------------


def prepare_file_for_speech(text: str, max_chars: int | None, *, max_words: int | None = None) -> list[Chunk]:
    """Parses a markdown file and packs it straight into speakable chunks.

    parse_markdown_blocks followed by chunk_blocks, for a caller that
    doesn't need the blocks themselves — handle_speak_file in
    app/handlers/voice.py."""
    return chunk_blocks(parse_markdown_blocks(text), max_chars, max_words=max_words)
