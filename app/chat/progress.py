"""TelegramProgress — renders agent step events into one live-edited message.

The generic ``baski.agents`` loop emits transport-agnostic ``AgentEvent``s; this
listener turns them into a single Telegram message, edited in place as the agent
works. While the model runs it shows a step log — each tool with a human label
(icon + verb) and its salient argument in a code span, thinking (a rotating
"думаю…" word when the model thinks without surfacing text), and short process
narration. Substantial prose the model writes *between* tool calls is kept as
content, not a status line — it accumulates into the reply body so nothing the
agent said is lost.

``finish()`` settles the message: the step log collapses into a Telegram
expandable blockquote (tucked under a cut), and the accumulated prose + final
answer follow below it. Only this side knows about aiogram, chat ids, MarkdownV2,
and Telegram's flood limits.
"""

import contextlib
import re
import time
from typing import NamedTuple, assert_never

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from baski.agents import (
    AgentEvent,
    Completed,
    Judged,
    Message,
    TextDelta,
    Thinking,
    ToolFinished,
    ToolStarted,
    TurnStarted,
)

from app.chat.format import split_message, strip_markdown_v2, to_markdown_v2

# Telegram rejects edits faster than ~1/sec per chat; match hermes' 0.5s cadence.
_EDIT_INTERVAL_S = 0.5
_MAX_LINES = 20
_THINKING_LIMIT = 120
_PREVIEW_LIMIT = 80
_CURSOR = " ▌"
# A Message shorter than this is process narration ("Поправляю формат…") and stays in the step
# log; anything longer is real content the model wrote and is kept in the reply body. Length is a
# rough proxy — substantial intermediate answers run long, throwaway narration stays terse.
_NARRATION_MAX = 200

# Tool-name keyword -> (icon, human label). First substring match wins, so order matters (the
# specific keys precede the general ones). Surfaces a tasteful step line instead of a raw tool name.
_TOOL_LABELS = (
    ("recall_read", "🧠", "Смотрю заметки"),
    ("recall", "🧠", "Память"),
    ("core_memory", "🧠", "Память"),
    ("ai_answer", "🔍", "Спрашиваю"),
    ("search", "🔍", "Ищу"),
    ("browse", "🌐", "Открываю"),
    ("list_show", "📋", "Смотрю список"),
    ("list", "✍️", "Список"),
    ("remind", "⏰", "Напоминаю"),
    ("schedule", "⏰", "Расписание"),
    ("research", "🔬", "Исследую"),
)

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


class _ToolLabel(NamedTuple):
    """How a tool's step line reads: an emoji icon and a human verb."""

    icon: str
    text: str


def _label(name: str) -> _ToolLabel:
    """The icon + human label for a tool name; falls back to `🔧` + the prettified name."""
    for keyword, icon, text in _TOOL_LABELS:
        if keyword in name:
            return _ToolLabel(icon, text)
    return _ToolLabel("🔧", name.replace("_", " "))


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
        self._tools: dict[str, tuple[int, str]] = {}  # tool name -> (step-log line index, arg preview)
        self._prose = ""  # substantial narration committed from tool-calling turns — kept as content
        self._answer = ""  # the current turn's text, streamed in sentence by sentence (live preview)
        self._think_idx = 0
        self._last_edit = 0.0

    async def __call__(self, event: AgentEvent) -> None:
        """Consume one agent event; reflect it in the live message (throttled)."""
        if isinstance(event, TurnStarted | Completed):
            return  # turn boundary / final answer (delivered by finish()) — nothing to render here
        if self._consume(event):
            await self._flush(force=False)

    def _consume(self, event: ToolStarted | ToolFinished | Thinking | TextDelta | Message | Judged) -> bool:
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
                self._commit_message(text)
            case Judged():
                return self._commit_judged(event)
            case _:
                assert_never(event)
        return True

    def _commit_judged(self, event: Judged) -> bool:
        """Self-check step — a visible step like a tool: the redo with its gaps, or a passed check."""
        if not event.finished:
            self._answer = ""  # the just-graded draft is being superseded — drop it from the live preview
            gaps = ", ".join(event.missing)[:_PREVIEW_LIMIT] if event.missing else "доделываю"
            self._lines.append(f"⚖️ Самопроверка: доделываю — {gaps}")
        elif event.attempt > 1:
            self._lines.append("⚖️ Самопроверка: ок (доработано)")
        else:
            self._lines.append("⚖️ Самопроверка: ок")
        return True

    def _commit_message(self, text: str) -> None:
        """Finalize a tool-calling turn's text: short narration to the step log, real content to the reply."""
        self._answer = ""  # this turn's streamed text is now finalized as `text`
        if len(text) <= _NARRATION_MAX:
            self._lines.append(f"💬 {text}")  # short process narration — a status line
        else:
            self._prose = f"{self._prose}\n\n{text}" if self._prose else text  # real content — keep it

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
        icon, label = _label(name)
        self._lines.append(self._tool_line(icon, label, preview, suffix="" if preview else "…"))
        self._tools[name] = (len(self._lines) - 1, preview)

    def _finish_tool(self, event: ToolFinished) -> None:
        """Mark this tool's in-flight line done, keeping its label and argument preview."""
        located = self._tools.get(event.name)
        if located is None:
            return
        idx, preview = located
        _, label = _label(event.name)
        mark = "✅" if event.ok else "⚠️"
        self._lines[idx] = self._tool_line(mark, label, preview, suffix=f" ({event.duration_ms / 1000:.1f}s)")

    @staticmethod
    def _tool_line(icon: str, label: str, preview: str, *, suffix: str) -> str:
        """One step-log line — `icon label `arg` suffix`, the arg as a markdown code span."""
        arg = f" `{preview}`" if preview else ""
        return f"{icon} {label}{arg}{suffix}"

    def _render(self) -> str:
        """The live message as markdown source: the step log, the kept prose, then the streaming reply."""
        parts = ["\n".join(self._lines[-_MAX_LINES:]), self._prose]
        if self._answer:
            parts.append(self._answer + _CURSOR)
        return "\n\n".join(p for p in parts if p)

    async def _flush(self, *, force: bool) -> None:
        """Render and deliver the live message, throttled to Telegram's edit cadence."""
        if not self._lines and not self._prose and not self._answer:
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
        """Settle the message: the step log collapses under a cut, the kept prose + answer follow.

        Reuses the live message and splits to the size limit. The step log stays atomic in the
        first chunk so the expandable blockquote is never cut mid-quote.
        """
        body = f"{self._prose}\n\n{answer}" if self._prose else answer
        chunks = split_message(to_markdown_v2(body))
        quote = self._steps_quote()
        chunks[0] = f"{quote}\n\n{chunks[0]}" if quote else chunks[0]
        for i, chunk in enumerate(chunks):
            # First chunk edits the live message in place; the rest are new messages.
            await self._send(chunk, edit=i == 0 and self._message_id is not None)

    def _steps_quote(self) -> str:
        """The step log as a MarkdownV2 blockquote, or '' when there were none.

        telegramify (``cite_expandable``, on by default) upgrades any blockquote over 200 chars to
        a collapsed/expandable one — so a multi-step log tucks under a cut, a one-liner stays plain.
        """
        lines = self._lines[-_MAX_LINES:]
        if not lines:
            return ""
        return to_markdown_v2("\n".join(f"> {line}" for line in lines))

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
