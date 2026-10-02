# Read-Aloud Test Document

A fixture for tests/test_markdown_blocks.py, and a handy file to exercise the
"speak this file" feature by hand: open it in the file explorer sidebar and
click its speaker icon. It isn't real project documentation.

## Basic formatting

This paragraph has **bold text**, _italic text_, and ~~strikethrough~~ mixed
into normal prose, plus a [link to the repo](https://github.com/example/repo)
and an inline code span like `requirements.txt`.

Identifiers that look like markdown but shouldn't be mangled: `get_user_profile`,
`__init__`, `__main__`, and a dotted filename like `config.yaml`.

## A list, to check the pause between items

- Fast
- Reliable
- Secure
- Ends without a period so a pause still has to be added automatically

And an ordered list:

1. Read the diff
2. Leave a comment
3. Click Finish Review

## A blockquote

> A single quoted line that should end with a clear pause before whatever
> comes next, even though the source has no trailing punctuation

## A code block (should be skipped, not read character by character)

```python
def greet(name):
    return f"Hello, {name}!"
```

## A table

| Step | Command |
| --- | --- |
| 1 | pip install -r requirements.txt |
| 2 | python run.py |

---

## Characters that should never reach the TTS model

Emoji and symbols: ✅ 🎉 🚀 — none of these should be audible or cause an
error. Control characters and stray unicode noise are stripped the same way.

## International text (should NOT be stripped)

café, déjà vu, Привет, 你好, Ünïcödé — all real prose, all preserved.

## A longer section, to force multiple TTS chunks

This paragraph is repeated a few times with small variations so the whole
document comfortably exceeds one chunk's character limit, which means
clicking the speaker icon here should produce several audio clips that
play back-to-back without a gap or restart, not just one.

This paragraph is repeated a few times with small variations so the whole
document comfortably exceeds one chunk's character limit, which means
clicking the speaker icon here should produce several audio clips that
play back-to-back without a gap or restart, not just one.

This paragraph is repeated a few times with small variations so the whole
document comfortably exceeds one chunk's character limit, which means
clicking the speaker icon here should produce several audio clips that
play back-to-back without a gap or restart, not just one.

This paragraph is repeated a few times with small variations so the whole
document comfortably exceeds one chunk's character limit, which means
clicking the speaker icon here should produce several audio clips that
play back-to-back without a gap or restart, not just one.

This paragraph is repeated a few times with small variations so the whole
document comfortably exceeds one chunk's character limit, which means
clicking the speaker icon here should produce several audio clips that
play back-to-back without a gap or restart, not just one.

That's the end of the document — the icon should revert from a stop
affordance back to a speaker icon shortly after this sentence finishes.
