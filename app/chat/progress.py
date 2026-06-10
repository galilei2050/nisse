"""TelegramProgress — renders agent step events into one live-edited message.

This is the concrete, Telegram-aware half of the step-notification seam. The
generic `baski.agents` loop emits transport-agnostic `AgentEvent`s; this listener
turns them into a single message that gets edited in place as the agent works.
Only this side knows about aiogram, chat ids, and Telegram's flood limits.
"""

import contextlib
import time
from typing import assert_never

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from baski.agents import AgentEvent, Completed, Message, Thinking, ToolFinished, ToolStarted, TurnStarted

from app.chat.format import split_message, strip_markdown_v2, to_markdown_v2

# Telegram rejects edits faster than ~1/sec per chat; match hermes' 0.5s cadence.
_EDIT_INTERVAL_S = 0.5
_MAX_LINES = 20
_THINKING_LIMIT = 120


def _pretty(name: str) -> str:
    """`google_search` -> `google search`."""
    return name.replace("_", " ")


def _brief(text: str) -> str:
    """First line of thinking, clipped — never dump the raw chain of thought."""
    head = text.strip().splitlines()[0] if text.strip() else ""
    return head[:_THINKING_LIMIT] + ("…" if len(head) > _THINKING_LIMIT else "")


class TelegramProgress:
    """Async listener that edits one Telegram message to show agent steps live."""

    def __init__(self, *, bot: Bot, chat_id: int) -> None:
        """Bind to the chat where the progress message lives."""
        self._bot = bot
        self._chat_id = chat_id
        self._message_id: int | None = None
        self._lines: list[str] = []
        self._last_edit: float = 0.0

    async def __call__(self, event: AgentEvent) -> None:
        """Consume one agent event and reflect it in the progress message."""
        match event:
            case TurnStarted() | Completed():
                return  # turn boundary / final answer (rendered by finish()) — nothing to render here
            case ToolStarted(name=name):
                self._lines.append(f"🔧 {_pretty(name)}…")
            case ToolFinished():
                self._finish_tool(event)
            case Thinking(text=text):
                brief = _brief(text)
                if not brief:
                    return
                self._lines.append(f"💭 {brief}")
            case Message(text=text):
                self._lines.append(f"💬 {text}")
            case _:
                assert_never(event)
        await self._flush(force=False)

    async def finish(self, answer: str) -> None:
        """Deliver the final answer as MarkdownV2, reusing the progress message and splitting if needed."""
        chunks = split_message(to_markdown_v2(answer))
        for i, chunk in enumerate(chunks):
            # First chunk edits the live progress message in place; the rest are new messages.
            await self._deliver(chunk, edit=i == 0 and self._message_id is not None)

    async def _deliver(self, text: str, *, edit: bool) -> None:
        """Send or edit one chunk — MarkdownV2 first, clean plain text on parse failure."""
        try:
            await self._put(text, edit=edit, parse_mode="MarkdownV2")
        except TelegramBadRequest:
            # Malformed entities, or "message is not modified" on edit — fall back to plain text.
            with contextlib.suppress(TelegramBadRequest):
                await self._put(strip_markdown_v2(text), edit=edit, parse_mode=None)

    async def _put(self, text: str, *, edit: bool, parse_mode: str | None) -> None:
        """Edit the progress message or send a new one with the given parse mode."""
        if edit:
            await self._bot.edit_message_text(
                text=text,
                chat_id=self._chat_id,
                message_id=self._message_id,
                parse_mode=parse_mode,
            )
        else:
            await self._bot.send_message(chat_id=self._chat_id, text=text, parse_mode=parse_mode)

    def _finish_tool(self, event: ToolFinished) -> None:
        """Mark the most recent in-flight line for this tool as done."""
        mark = "✅" if event.ok else "⚠️"
        secs = event.duration_ms / 1000
        prefix = f"🔧 {_pretty(event.name)}…"
        for i in range(len(self._lines) - 1, -1, -1):
            if self._lines[i] == prefix:
                self._lines[i] = f"{mark} {_pretty(event.name)} ({secs:.1f}s)"
                return

    async def _flush(self, *, force: bool) -> None:
        """Send or edit the progress message, throttled unless forced."""
        if not self._lines:
            return
        now = time.monotonic()
        if not force and now - self._last_edit < _EDIT_INTERVAL_S:
            return
        self._last_edit = now
        text = "\n".join(self._lines[-_MAX_LINES:])

        if self._message_id is None:
            sent = await self._bot.send_message(chat_id=self._chat_id, text=text)
            self._message_id = sent.message_id
            return
        try:
            await self._bot.edit_message_text(text=text, chat_id=self._chat_id, message_id=self._message_id)
        except TelegramRetryAfter as e:
            self._last_edit = now + e.retry_after  # back off; next event will retry
        except TelegramBadRequest:
            pass  # "message is not modified" — nothing changed since last edit
