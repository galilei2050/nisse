"""TelegramProgress — renders agent step events into one live-edited message.

The generic ``baski.agents`` loop emits transport-agnostic ``AgentEvent``s; this
listener turns them into a single Telegram message, edited in place as the agent
works. While the model runs it shows a step log — each tool with a human label
(icon + verb) and its salient argument in a code span, thinking (a rotating
"думаю…" word when the model thinks without surfacing text), and short process
narration. Substantial prose the model writes *between* tool calls is kept as
content, not a status line — it accumulates into the reply body so nothing the
agent said is lost.

``finish()`` settles the message: the step log renders as a Telegram blockquote
and the accumulated prose + final answer follow below it, all in chronological
order. Only this side knows about aiogram, chat ids, MarkdownV2, and Telegram's
flood limits.
"""

import contextlib
import re
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import NamedTuple, assert_never

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from baski.agents import (
    AgentEvent,
    AgentExecuteResult,
    Completed,
    Judged,
    Message,
    TextDelta,
    Thinking,
    ToolFinished,
    ToolStarted,
    TurnStarted,
)

from app.chat.format import NO_ANSWER, footer, split_message, strip_markdown_v2, to_markdown_v2, verdict_line

# Telegram rejects edits faster than ~1/sec per chat; match hermes' 0.5s cadence.
_EDIT_INTERVAL_S = 0.5
_THINKING_LIMIT = 120
_PREVIEW_LIMIT = 80
_CURSOR = " ▌"

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


@dataclass
class _Seg:
    """One chronological block of the message: a process group (tools/thinking), model text, or a verdict.

    The whole message is an ordered list of these, so tools, text, and judge verdicts stay interleaved
    in the order they happened — nothing is split into a separate stream or dropped.
    """

    kind: str  # "process" | "text" | "judge"
    lines: list[str] = field(default_factory=list)


class TelegramProgress:
    """Async listener that edits one Telegram message to show agent steps, then streams the reply."""

    def __init__(self, *, bot: Bot, chat_id: int) -> None:
        """Bind to the chat where the progress message lives."""
        self._bot = bot
        self._chat_id = chat_id
        self._message_ids: list[int] = []  # every message sent here, in order; [0] is the live-edited one
        self._segments: list[_Seg] = []  # the chronological stream: process / text / judge blocks, in order
        # Tool name -> the lines its still-running calls wrote, oldest first: (segment idx, line idx,
        # preview). A queue, not one slot, because one turn routinely calls the same tool several
        # times at once (nine `update_hypothesis` in a row happens) and each call owns its own line.
        self._tool_loc: dict[str, deque[tuple[int, int, str]]] = defaultdict(deque)
        self._answer = ""  # the current turn's text, streamed in (live preview); committed into a segment
        self._think_idx = 0
        self._last_edit = 0.0

    @property
    def message_ids(self) -> list[int]:
        """The Telegram messages this reply was delivered in — what a later reaction lands on."""
        return self._message_ids

    def _process(self) -> _Seg:
        """The open process block to append a tool/thinking line to — a fresh one after any text/judge."""
        if not self._segments or self._segments[-1].kind != "process":
            self._segments.append(_Seg("process"))
        return self._segments[-1]

    async def __call__(self, event: AgentEvent) -> None:
        """Consume one agent event; reflect it in the live message (throttled)."""
        if isinstance(event, TurnStarted | Completed):
            return  # turn boundary / final answer (delivered by finish()) — nothing to render here
        if self._consume(event):
            # Force an edit on tool boundaries so a long-running call (a sub-agent minutes deep) always
            # shows its in-flight then ✅-done line — never a frozen cursor while it works.
            await self._flush(force=isinstance(event, ToolStarted | ToolFinished))

    def _consume(self, event: ToolStarted | ToolFinished | Thinking | TextDelta | Message | Judged) -> bool:
        """Apply the event to the render state; return whether it warrants an edit now."""
        match event:
            case ToolStarted(name=name, tool_input=tool_input):
                self._start_tool(name, _preview(tool_input))
            case ToolFinished():
                self._finish_tool(event)
            case Thinking(text=text):
                self._process().lines.append(f"💭 {self._thinking(text)}")
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
        """Self-check verdict, placed right after the text it graded — so the chronology reads text→verdict.

        The graded draft is committed as text FIRST (never wiped — it's the model's pre-check text and
        often carries content the rewrite drops), then the verdict line follows it in the stream.
        """
        self._commit_answer()  # the model's text came before this verdict — keep it, in order
        self._segments.append(_Seg("judge", [verdict_line(event)]))  # `_render_seg` supplies the bold
        return True

    def _commit_message(self, text: str) -> None:
        """Commit a tool-calling turn's narration as a text block, in order — nothing the model writes is dropped."""
        self._answer = ""  # this turn's streamed text is finalized as `text`
        if text:
            self._segments.append(_Seg("text", [text]))

    def _commit_answer(self) -> None:
        """Move the streamed live text into a committed text block (kept in chronological order)."""
        if self._answer:
            self._segments.append(_Seg("text", [self._answer]))
            self._answer = ""

    def _thinking(self, text: str) -> str:
        """A thinking line's content: the brief if there is one, else a rotating "thinking…" word."""
        brief = _brief(text)
        if brief:
            return brief
        word = _THINKING_WORDS[self._think_idx % len(_THINKING_WORDS)]
        self._think_idx += 1
        return f"{word}…"

    def _start_tool(self, name: str, preview: str) -> None:
        """Append an in-flight tool line to the current process block; remember it for ToolFinished."""
        icon, label = _label(name)
        seg = self._process()
        seg.lines.append(self._tool_line(icon, label, preview, suffix="" if preview else "…"))
        self._tool_loc[name].append((len(self._segments) - 1, len(seg.lines) - 1, preview))

    def _finish_tool(self, event: ToolFinished) -> None:
        """Mark the finished call's in-flight line done in place, keeping its label and argument preview.

        Oldest line first: `ToolFinished` carries only the tool's name, but baski runs a batch through
        `asyncio.gather` and emits one finish per result in the order the calls were started, so the
        n-th finish of a name closes the n-th line that name opened.
        """
        pending = self._tool_loc.get(event.name)
        if not pending:
            return
        seg_idx, line_idx, preview = pending.popleft()
        _, label = _label(event.name)
        mark = "✅" if event.ok else "⚠️"
        suffix = f" ({event.duration_ms / 1000:.1f}s)"
        self._segments[seg_idx].lines[line_idx] = self._tool_line(mark, label, preview, suffix=suffix)

    @staticmethod
    def _tool_line(icon: str, label: str, preview: str, *, suffix: str) -> str:
        """One step-log line — `icon label `arg` suffix`, the arg as a markdown code span."""
        arg = f" `{preview}`" if preview else ""
        return f"{icon} {label}{arg}{suffix}"

    def _render(self) -> str:
        """The message as markdown source: every segment in order, then the live streaming reply."""
        parts = [self._render_seg(seg) for seg in self._segments]
        if self._answer:
            parts.append(self._answer + _CURSOR)
        return "\n\n".join(p for p in parts if p)

    @staticmethod
    def _render_seg(seg: _Seg) -> str:
        """Process → collapsible blockquote; judge → bold (emphasized verdict); text → plain."""
        if seg.kind == "process":
            return "\n".join(f"> {line}" for line in seg.lines)
        if seg.kind == "judge":
            return "\n".join(f"**{line}**" for line in seg.lines)
        return "\n".join(seg.lines)

    async def _flush(self, *, force: bool) -> None:
        """Render and deliver the live message, throttled to Telegram's edit cadence."""
        if not self._segments and not self._answer:
            return
        now = time.monotonic()
        if not force and now - self._last_edit < _EDIT_INTERVAL_S:
            return
        self._last_edit = now
        try:
            await self._send(to_markdown_v2(self._render()), edit=bool(self._message_ids))
        except TelegramRetryAfter as e:
            self._last_edit = now + e.retry_after  # back off; the next event retries

    async def finish(self, result: AgentExecuteResult) -> None:
        """Settle the message: the full chronological stream (tools/text/verdicts) + cost footer.

        Reuses the live message and splits to the size limit. Each process block is one paragraph, so
        `split_message` keeps it atomic — a blockquote is never cut mid-quote.
        """
        self._commit_answer()  # commit any trailing streamed text (the judge usually has already)
        if not any(seg.kind == "text" for seg in self._segments):
            self._segments.append(_Seg("text", [NO_ANSWER]))  # the agent produced no answer text
        await self._settle(f"{self._render()}{footer(result)}")

    async def finish_text(self, text: str) -> None:
        """Settle with a plain message (error/refusal paths that have no result), keeping the steps so far."""
        self._commit_answer()
        await self._settle(f"{self._render()}\n\n{text}" if self._segments else text)

    async def _settle(self, body: str) -> None:
        """Convert, size-split, and deliver the final body — first chunk edits in place, rest are new."""
        for i, chunk in enumerate(split_message(to_markdown_v2(body))):
            await self._send(chunk, edit=i == 0 and bool(self._message_ids))

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
        """Edit the live message or send a new one; every send is remembered, the first one is edited."""
        if edit and self._message_ids:
            await self._bot.edit_message_text(
                text=text, chat_id=self._chat_id, message_id=self._message_ids[0], parse_mode=parse_mode
            )
            return
        sent = await self._bot.send_message(chat_id=self._chat_id, text=text, parse_mode=parse_mode)
        self._message_ids.append(sent.message_id)
