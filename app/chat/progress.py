"""TelegramProgress — renders agent step events into one live-edited message.

The generic ``baski.agents`` loop emits transport-agnostic ``AgentEvent``s; this
listener turns them into a single Telegram message, edited in place as the agent
works. While the model runs it shows a step checklist — each tool with its salient
argument in a code span, and thinking (a rotating "думаю…" word when the model
thinks without surfacing text). Once the reply text starts arriving it streams in,
edited a sentence at a time. Only this side knows about aiogram, chat ids,
MarkdownV2, and Telegram's flood limits.
"""

import contextlib
import re
import time
from typing import assert_never

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from baski.agents import AgentEvent, Completed, Message, TextDelta, Thinking, ToolFinished, ToolStarted, TurnStarted

from app.chat.format import split_message, strip_markdown_v2, to_markdown_v2

# Telegram rejects edits faster than ~1/sec per chat; match hermes' 0.5s cadence.
_EDIT_INTERVAL_S = 0.5
_MAX_LINES = 20
_THINKING_LIMIT = 120
_PREVIEW_LIMIT = 80
_CURSOR = " ▌"

# Shown (rotating) when the model thinks but surfaces no readable text — Cyrillic + Latin scripts.
_THINKING_WORDS = (
    "думаю",
    "размышляю",
    "кумекаю",
    "мізкую",
    "thinking",
    "pondering",
    "musing",
    "pensando",
    "réfléchis",
    "denke nach",
    "myślę",
    "考え中",
)

# A finished sentence: ends with . ! ? … or a newline, optionally trailed by a closing quote/bracket.
_SENTENCE_END = re.compile(r"[.!?…\n][\"»”’)\]]*\s*$")

# Salient argument to preview for a tool call: first of these keys present, else the first string value.
_PREVIEW_KEYS = ("query", "url", "title", "text", "body", "message", "name", "public_id")


def _pretty(name: str) -> str:
    """`google_search` -> `google search`."""
    return name.replace("_", " ")


def _brief(text: str) -> str:
    """First line of thinking, clipped — never dump the raw chain of thought."""
    head = text.strip().splitlines()[0] if text.strip() else ""
    return head[:_THINKING_LIMIT] + ("…" if len(head) > _THINKING_LIMIT else "")


def _preview(tool_input: dict[str, object]) -> str:
    """One-line preview of a tool's salient argument, fit for a markdown code span."""
    for key in _PREVIEW_KEYS:
        candidate = tool_input.get(key)
        if isinstance(candidate, str) and candidate.strip():
            value = candidate
            break
    else:
        value = next((v for v in tool_input.values() if isinstance(v, str) and v.strip()), "")
    value = " ".join(value.split())  # collapse newlines/runs of whitespace to a single line
    clipped = value[:_PREVIEW_LIMIT] + ("…" if len(value) > _PREVIEW_LIMIT else "")
    return clipped.replace("`", "'")  # a stray backtick would break the code span


class TelegramProgress:
    """Async listener that edits one Telegram message to show agent steps, then streams the reply."""

    def __init__(self, *, bot: Bot, chat_id: int) -> None:
        """Bind to the chat where the progress message lives."""
        self._bot = bot
        self._chat_id = chat_id
        self._message_id: int | None = None
        self._lines: list[str] = []
        self._tools: dict[str, tuple[int, str]] = {}  # tool name -> (checklist line index, arg preview)
        self._answer = ""  # the reply, accumulated from text deltas and streamed in sentence by sentence
        self._think_idx = 0
        self._last_edit = 0.0

    async def __call__(self, event: AgentEvent) -> None:
        """Consume one agent event; reflect it in the live message (throttled)."""
        if isinstance(event, TurnStarted | Completed):
            return  # turn boundary / final answer (delivered by finish()) — nothing to render here
        if self._consume(event):
            await self._flush(force=False)

    def _consume(self, event: ToolStarted | ToolFinished | Thinking | TextDelta | Message) -> bool:
        """Apply the event to the render state; return whether it warrants an edit now."""
        match event:
            case ToolStarted(name=name, tool_input=tool_input):
                self._start_tool(name, _preview(tool_input))
            case ToolFinished():
                self._finish_tool(event)
            case Thinking(text=text):
                self._lines.append(f"💭 {self._thinking(text)}")
            case TextDelta(text=text):
                self._answer += text
                return bool(_SENTENCE_END.search(self._answer))  # hold the edit until a sentence completes
            case Message(text=text):
                self._lines.append(f"💬 {text}")
                self._answer = ""  # narration is now a committed line; the final reply streams fresh
            case _:
                assert_never(event)
        return True

    def _thinking(self, text: str) -> str:
        """A thinking line's content: the brief if there is one, else a rotating "thinking…" word."""
        brief = _brief(text)
        if brief:
            return brief
        word = _THINKING_WORDS[self._think_idx % len(_THINKING_WORDS)]
        self._think_idx += 1
        return f"{word}…"

    def _start_tool(self, name: str, preview: str) -> None:
        """Append an in-flight tool line and remember it so ToolFinished can mark it done in place."""
        self._lines.append(self._tool_line("🔧", name, preview, suffix="" if preview else "…"))
        self._tools[name] = (len(self._lines) - 1, preview)

    def _finish_tool(self, event: ToolFinished) -> None:
        """Mark this tool's in-flight line done, keeping its argument preview."""
        located = self._tools.get(event.name)
        if located is None:
            return
        idx, preview = located
        mark = "✅" if event.ok else "⚠️"
        self._lines[idx] = self._tool_line(mark, event.name, preview, suffix=f" ({event.duration_ms / 1000:.1f}s)")

    @staticmethod
    def _tool_line(icon: str, name: str, preview: str, *, suffix: str) -> str:
        """One checklist line — `icon tool name `arg` suffix`, the arg as a markdown code span."""
        arg = f" `{preview}`" if preview else ""
        return f"{icon} {_pretty(name)}{arg}{suffix}"

    def _render(self) -> str:
        """The message body as markdown source: the step checklist, then the streaming reply."""
        body = "\n".join(self._lines[-_MAX_LINES:])
        if self._answer:
            answer = self._answer + _CURSOR
            body = f"{body}\n\n{answer}" if body else answer
        return body

    async def _flush(self, *, force: bool) -> None:
        """Render and deliver the live message, throttled to Telegram's edit cadence."""
        if not self._lines and not self._answer:
            return
        now = time.monotonic()
        if not force and now - self._last_edit < _EDIT_INTERVAL_S:
            return
        self._last_edit = now
        try:
            await self._send(to_markdown_v2(self._render()), edit=self._message_id is not None)
        except TelegramRetryAfter as e:
            self._last_edit = now + e.retry_after  # back off; the next event retries

    async def finish(self, answer: str) -> None:
        """Deliver the final answer as MarkdownV2, reusing the live message and splitting if needed."""
        chunks = split_message(to_markdown_v2(answer))
        for i, chunk in enumerate(chunks):
            # First chunk edits the live message in place; the rest are new messages.
            await self._send(chunk, edit=i == 0 and self._message_id is not None)

    async def _send(self, text: str, *, edit: bool) -> None:
        """Edit/send already-converted MarkdownV2 *text*, falling back to plain text on a parse error."""
        try:
            await self._put(text, edit=edit, parse_mode="MarkdownV2")
        except TelegramBadRequest as e:
            if "not modified" in str(e).lower():
                return  # nothing changed since the last edit
            with contextlib.suppress(TelegramBadRequest):
                await self._put(strip_markdown_v2(text), edit=edit, parse_mode=None)

    async def _put(self, text: str, *, edit: bool, parse_mode: str | None) -> None:
        """Edit the live message or send a new one; the first send captures the message id to edit."""
        message_id = self._message_id
        if edit and message_id is not None:
            await self._bot.edit_message_text(
                text=text, chat_id=self._chat_id, message_id=message_id, parse_mode=parse_mode
            )
            return
        sent = await self._bot.send_message(chat_id=self._chat_id, text=text, parse_mode=parse_mode)
        if self._message_id is None:
            self._message_id = sent.message_id
