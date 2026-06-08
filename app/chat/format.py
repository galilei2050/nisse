"""Render an LLM markdown answer as Telegram MarkdownV2, split to the size limit.

``Assistant.reply()`` returns standard markdown (``**bold**``, ``## headers``,
fenced code, links, lists). :func:`to_markdown_v2` converts it to Telegram
MarkdownV2 via the ``telegramify-markdown`` library (a real pulldown-cmark
parser, not regex); :func:`split_message` keeps each message under Telegram's
UTF-16 limit; :func:`strip_markdown_v2` is the plain-text fallback when Telegram
rejects the converted entities.
"""

import re

import telegramify_markdown

__all__ = ["split_message", "strip_markdown_v2", "to_markdown_v2"]

# Telegram's per-message limit, counted in UTF-16 code units.
MAX_MESSAGE_LENGTH = 4096

# Sentence end: . ! ? — optionally MarkdownV2-escaped (\.) — then whitespace.
_SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s+|(?<=\\[.!?])\s+")

# A thematic-break line (---, ***, ___). telegramify renders it as a dash rule;
# we drop it instead — a separator carries no meaning in a chat answer.
_HR_RE = re.compile(r"^[ \t]*(?:-{3,}|\*{3,}|_{3,})[ \t]*$\n?", re.MULTILINE)


def to_markdown_v2(content: str) -> str:
    """Convert standard markdown to Telegram MarkdownV2, dropping thematic breaks."""
    if not content:
        return content
    return telegramify_markdown.markdownify(_HR_RE.sub("", content))


def strip_markdown_v2(text: str) -> str:
    """Drop MarkdownV2 escapes and markers, yielding clean plain text."""
    text = re.sub(r"\\([_*\[\]()~`>#+\-=|{}.!\\])", r"\1", text)
    text = re.sub(r"\*([^*]+)\*", r"\1", text)
    # Word-boundary guards keep snake_case identifiers intact.
    text = re.sub(r"(?<!\w)_([^_]+)_(?!\w)", r"\1", text)
    return re.sub(r"~([^~]+)~", r"\1", text)


def _utf16_len(text: str) -> int:
    """Length in UTF-16 code units — the unit Telegram counts messages in."""
    return len(text.encode("utf-16-le")) // 2


def _atomic_blocks(text: str) -> list[str]:
    """Split into paragraphs, keeping each fenced code block as one unit."""
    blocks: list[str] = []
    for segment in re.split(r"(```[\s\S]*?```)", text):
        if segment.startswith("```"):
            blocks.append(segment)
        else:
            blocks.extend(p for p in segment.split("\n\n") if p)
    return blocks


def _hard_chunks(text: str, limit: int) -> list[str]:
    """Last resort: cut *text* into ``limit``-sized pieces by UTF-16 length."""
    chunks, current = [], ""
    for ch in text:
        if _utf16_len(current + ch) > limit:
            chunks.append(current)
            current = ch
        else:
            current += ch
    if current:
        chunks.append(current)
    return chunks


def _split_oversized(block: str, limit: int) -> list[str]:
    """Split a single over-limit block by sentences, then hard-cut what remains."""
    pieces: list[str] = []
    for sentence in _SENTENCE_END_RE.split(block):
        if _utf16_len(sentence) > limit:
            pieces.extend(_hard_chunks(sentence, limit))
        else:
            pieces.append(sentence)
    return pieces


def split_message(text: str, limit: int = MAX_MESSAGE_LENGTH) -> list[str]:
    """Split *text* into messages under Telegram's UTF-16 *limit*.

    Greedily packs whole paragraphs; a paragraph that alone exceeds *limit* is
    broken on sentence boundaries, and a single oversized sentence (or code
    block) is hard-cut. Returns at least one chunk.
    """
    if _utf16_len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current = ""
    for block in _atomic_blocks(text):
        pieces = [block] if _utf16_len(block) <= limit else _split_oversized(block, limit)
        for piece in pieces:
            candidate = f"{current}\n\n{piece}" if current else piece
            if current and _utf16_len(candidate) > limit:
                chunks.append(current)
                current = piece
            else:
                current = candidate
    if current:
        chunks.append(current)
    return chunks
