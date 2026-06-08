"""Render an LLM markdown answer as Telegram MarkdownV2, split to the size limit.

``Assistant.reply()`` returns standard markdown (``**bold**``, ``## headers``,
fenced code, links, lists). Telegram speaks MarkdownV2, which escapes a different
character set and uses single ``*``/``_`` markers. :func:`to_markdown_v2` converts
between the two with code regions protected; :func:`split_message` keeps each
message under Telegram's UTF-16 limit; :func:`strip_markdown_v2` is the plain-text
fallback when Telegram rejects the converted entities.
"""

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = ["split_message", "strip_markdown_v2", "to_markdown_v2"]

# Telegram's per-message limit, counted in UTF-16 code units.
MAX_MESSAGE_LENGTH = 4096

# Every character MarkdownV2 requires backslash-escaped outside a code span.
_MDV2_ESCAPE_RE = re.compile(r"([_*\[\]()~`>#+\-=|{}.!\\])")

# Sentence end: . ! ? — optionally MarkdownV2-escaped (\.) — then whitespace.
_SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s+|(?<=\\[.!?])\s+")


def _escape_mdv2(text: str) -> str:
    """Escape Telegram MarkdownV2 special characters with a preceding backslash."""
    return _MDV2_ESCAPE_RE.sub(r"\\\1", text)


def _utf16_len(text: str) -> int:
    """Length in UTF-16 code units — the unit Telegram counts messages in."""
    return len(text.encode("utf-16-le")) // 2


class _Placeholders:
    """Stash protected regions behind NUL-delimited tokens that survive escaping."""

    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    def stash(self, value: str) -> str:
        """Replace *value* with a token, remembering it for :meth:`restore`."""
        key = f"\x00PH{len(self._store)}\x00"
        self._store[key] = value
        return key

    def restore(self, text: str) -> str:
        """Put every stashed value back, latest first so nested tokens resolve."""
        for key in reversed(self._store):
            text = text.replace(key, self._store[key])
        return text


def _protect_fenced(m: re.Match[str], stash: "Callable[[str], str]") -> str:
    raw = m.group(0)
    # Keep the opening fence (with optional language) and closing fence as-is;
    # inside the body only \ and ` need escaping per the MarkdownV2 spec.
    open_end = raw.index("\n") + 1 if "\n" in raw[3:] else 3
    body = raw[open_end:-3].replace("\\", "\\\\").replace("`", "\\`")
    return stash(raw[:open_end] + body + "```")


def _convert_link(m: re.Match[str], stash: "Callable[[str], str]") -> str:
    display = _escape_mdv2(m.group(1))
    url = m.group(2).replace("\\", "\\\\").replace(")", "\\)")
    return stash(f"[{display}]({url})")


def _convert_header(m: re.Match[str], stash: "Callable[[str], str]") -> str:
    inner = re.sub(r"\*\*(.+?)\*\*", r"\1", m.group(1).strip())
    return stash(f"*{_escape_mdv2(inner)}*")


def to_markdown_v2(content: str) -> str:
    """Convert standard markdown to Telegram MarkdownV2.

    Code blocks and inline code are stashed behind placeholder tokens so their
    contents are never escaped or reformatted; headers/bold/italic/strikethrough
    and links are translated to MarkdownV2 syntax; everything else is escaped.
    """
    if not content:
        return content
    ph = _Placeholders()
    stash = ph.stash

    text = re.sub(r"(```(?:[^\n]*\n)?[\s\S]*?```)", lambda m: _protect_fenced(m, stash), content)
    text = re.sub(r"(`[^`]+`)", lambda m: stash(m.group(0).replace("\\", "\\\\")), text)
    text = re.sub(r"\[([^\]]+)\]\(([^()]*(?:\([^()]*\)[^()]*)*)\)", lambda m: _convert_link(m, stash), text)
    text = re.sub(r"^#{1,6}\s+(.+)$", lambda m: _convert_header(m, stash), text, flags=re.MULTILINE)
    text = re.sub(r"\*\*(.+?)\*\*", lambda m: stash(f"*{_escape_mdv2(m.group(1))}*"), text)
    # [^*\n]+ stops italic at newlines so bullet lists ("* item") aren't consumed.
    text = re.sub(r"\*([^*\n]+)\*", lambda m: stash(f"_{_escape_mdv2(m.group(1))}_"), text)
    text = re.sub(r"~~(.+?)~~", lambda m: stash(f"~{_escape_mdv2(m.group(1))}~"), text)

    return ph.restore(_escape_mdv2(text))


def strip_markdown_v2(text: str) -> str:
    """Drop MarkdownV2 escapes and markers, yielding clean plain text."""
    text = re.sub(r"\\([_*\[\]()~`>#+\-=|{}.!\\])", r"\1", text)
    text = re.sub(r"\*([^*]+)\*", r"\1", text)
    # Word-boundary guards keep snake_case identifiers intact.
    text = re.sub(r"(?<!\w)_([^_]+)_(?!\w)", r"\1", text)
    return re.sub(r"~([^~]+)~", r"\1", text)


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
