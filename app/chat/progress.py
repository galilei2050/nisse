"""TelegramProgress — renders agent step events into one live-edited message.

This is the concrete, Telegram-aware half of the step-notification seam. The
generic `baski.agents` loop emits transport-agnostic `AgentEvent`s; this listener
turns them into a single message that gets edited in place as the agent works.
Only this side knows about aiogram, chat ids, and Telegram's flood limits.
"""

import contextlib
import time

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from baski.agents import AgentEvent, Thinking, ToolFinished, ToolStarted

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
        if isinstance(event, ToolStarted):
            self._lines.append(f"🔧 {_pretty(event.name)}…")
        elif isinstance(event, ToolFinished):
            self._finish_tool(event)
        elif isinstance(event, Thinking):
            brief = _brief(event.text)
            if brief:
                self._lines.append(f"💭 {brief}")
        else:
            return  # TurnStarted / Completed — nothing to show on their own
        await self._flush(force=False)

    async def finish(self, answer: str) -> None:
        """Replace the live progress with the final answer (edit, not a new message)."""
        if self._message_id is None:
            await self._bot.send_message(chat_id=self._chat_id, text=answer)
            return
        # TelegramBadRequest if the answer is identical to the last progress edit — nothing to change.
        with contextlib.suppress(TelegramBadRequest):
            await self._bot.edit_message_text(text=answer, chat_id=self._chat_id, message_id=self._message_id)

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
