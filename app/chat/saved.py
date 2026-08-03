"""Read-only viewer over everything the agent saved — `/lists`, `/memory`, `/core`, `/schedules`.

The owner can only see his saved state by asking the agent for it, which costs a model turn and
returns whatever the agent chose to summarize. These commands read the four stores directly and
render them verbatim: no model call, no tokens, no paraphrase.

Design follows Telegram's own guidance (core.telegram.org/bots/features):
- **One command per store**, not `/show <what>` — "commands should be as specific as possible".
  Each is published via `set_my_commands` (BOT_COMMANDS), so `/` autocomplete and the menu button
  list them with Russian descriptions; discovery needs no `/help`.
- **Drill-down edits the message in place** rather than sending a new one ("both faster and
  smoother"): the index of entries is a keyboard, a tap replaces the text with that entry's content
  plus a Back button, Back restores the index.
- Only the two unbounded stores (lists, memories) get the index/drill-down; core memory is one
  capped block and the schedule list is a handful of lines, so those just print.

Plain text, no MarkdownV2: the content is the owner's own words (list items, memory bodies) and is
shown byte-for-byte, with no escaping layer that could mangle or reject it.
"""

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import NamedTuple

from aiogram import Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.filters.callback_data import CallbackData
from aiogram.types import BotCommand, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from pymongo.asynchronous.database import AsyncDatabase

from app.chat.format import split_message
from app.lists import ItemList, ListStore
from app.memory import MemoryStore
from app.memory.store import Memory, MemoryCategory, SourceKind
from app.prompts import PromptStore, PromptType
from app.scheduling import ScheduleStore
from app.scheduling.store import ScheduledTask, ScheduleKind

logger = logging.getLogger(__name__)

_PAGE_SIZE = 8  # entries per index page — fits on one screen without scrolling the keyboard
_LABEL_LIMIT = 40  # button captions Telegram still renders on a single line

BOT_COMMANDS = [
    BotCommand(command="lists", description="📋 Списки"),
    BotCommand(command="memory", description="🧠 Заметки — что бот запомнил"),
    BotCommand(command="core", description="⭐ Постоянная память"),
    BotCommand(command="schedules", description="⏰ Напоминания и рутины"),
]

_CATEGORY_RU = {MemoryCategory.FACT: "факт", MemoryCategory.EVENT: "событие"}
_SOURCE_RU = {
    SourceKind.USER: "с твоих слов",
    SourceKind.EXTERNAL: "из внешнего источника",
    SourceKind.AGENT: "бот сам",
}


class SavedKind(StrEnum):
    """Which store an index/entry view is showing."""

    LISTS = "lists"
    MEMORY = "memory"


class SavedCallback(CallbackData, prefix="saved"):
    """Button payload: which store, which entry (-1 = back to the index), which index page."""

    kind: SavedKind
    idx: int
    page: int


@dataclass(frozen=True, slots=True)
class _Entry:
    """One browsable record: its button caption in the index and the text a tap opens."""

    label: str
    body: str


class _View(NamedTuple):
    """What one message shows after a command or a tap: its text and its keyboard (None = no buttons)."""

    text: str
    markup: InlineKeyboardMarkup | None


_INDEX_TITLE = {SavedKind.LISTS: "📋 Списки", SavedKind.MEMORY: "🧠 Заметки"}
_INDEX_EMPTY = {
    SavedKind.LISTS: "📋 Списков пока нет — попроси меня что-нибудь записать.",
    SavedKind.MEMORY: "🧠 Заметок пока нет — я ещё ничего не запомнил.",
}


def _clip(text: str, limit: int) -> str:
    """Shorten to *limit* characters with an ellipsis, for a one-line button caption."""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _render_list(item: ItemList) -> str:
    """One list, numbered — the whole thing, in stored order."""
    body = "\n".join(f"{n}. {text}" for n, text in enumerate(item.items, 1)) or "(пусто)"
    return f"📋 {item.name} — {len(item.items)} шт.\n\n{body}"


def _render_memory(memory: Memory) -> str:
    """One memory: title, what kind it is, when it was last touched, where it came from, body."""
    meta = f"{_CATEGORY_RU[memory.category]} · {memory.updated_at:%d.%m.%Y} · {_SOURCE_RU[memory.source.kind]}"
    ref = f"\n🔗 {memory.source.ref}" if memory.source.ref else ""
    return f"🧠 {memory.title}\n{meta}{ref}\n\n{memory.body}"


def _render_schedule(task: ScheduledTask) -> str:
    """One armed task: when it fires (UTC — the bot stores no local timezone) and how often."""
    every = f" · каждые {task.repeat_every_hours} ч" if task.kind is ScheduleKind.RECURRING else ""
    return f"⏰ {task.fire_at:%d.%m.%Y %H:%M} UTC{every}\n{task.instruction}"


def _index_view(kind: SavedKind, entries: list[_Entry], page: int) -> _View:
    """The index of one store: a header line plus one button per entry on this page."""
    if not entries:
        return _View(_INDEX_EMPTY[kind], None)
    pages = (len(entries) + _PAGE_SIZE - 1) // _PAGE_SIZE
    page = min(max(page, 0), pages - 1)
    start = page * _PAGE_SIZE
    rows = [
        [InlineKeyboardButton(text=entry.label, callback_data=SavedCallback(kind=kind, idx=i, page=page).pack())]
        for i, entry in enumerate(entries[start : start + _PAGE_SIZE], start)
    ]
    nav = [
        InlineKeyboardButton(text=text, callback_data=SavedCallback(kind=kind, idx=-1, page=target).pack())
        for text, target, shown in (("‹ Назад", page - 1, page > 0), ("Вперёд ›", page + 1, page < pages - 1))
        if shown
    ]
    if nav:
        rows.append(nav)
    header = f"{_INDEX_TITLE[kind]} — {len(entries)}"
    if pages > 1:
        header = f"{header} · стр. {page + 1}/{pages}"
    return _View(header, InlineKeyboardMarkup(inline_keyboard=rows))


def _entry_view(kind: SavedKind, entries: list[_Entry], idx: int, page: int) -> _View:
    """One opened entry plus a Back button.

    Buttons carry the entry's position, not its id — a name or title would blow the 64-byte callback
    payload. The store is re-read on every tap, so an entry saved or removed between render and tap
    shifts the positions; an index that no longer exists falls back to the (freshly read) list.
    """
    if not 0 <= idx < len(entries):
        return _index_view(kind, entries, page)
    back = InlineKeyboardButton(text="⬅️ Назад", callback_data=SavedCallback(kind=kind, idx=-1, page=page).pack())
    return _View(entries[idx].body, InlineKeyboardMarkup(inline_keyboard=[[back]]))


class SavedViewer:
    """Telegram command handlers that show the four stores. Lifecycle: long-lived (one per bot)."""

    def __init__(self, database: AsyncDatabase) -> None:
        """Hold the database the four stores are opened on, per chat, at request time."""
        self._database = database

    def register(self, router: Router) -> None:
        """Wire the viewer's commands and button taps onto the chat router.

        Must run BEFORE the catch-all message handler is registered: aiogram tries handlers in
        registration order, so a later catch-all would swallow the commands into an agent turn.
        """
        router.message.register(self.show_lists, Command("lists"))
        router.message.register(self.show_memory, Command("memory"))
        router.message.register(self.show_core, Command("core"))
        router.message.register(self.show_schedules, Command("schedules"))
        router.callback_query.register(self.tap, SavedCallback.filter())

    async def show_lists(self, message: Message) -> None:
        """`/lists` — the named lists in this chat, one button each."""
        await self._show_index(message, SavedKind.LISTS)

    async def show_memory(self, message: Message) -> None:
        """`/memory` — the long-term notes in this chat, newest first, one button each."""
        await self._show_index(message, SavedKind.MEMORY)

    async def show_core(self, message: Message) -> None:
        """`/core` — the always-on block that shapes every reply, verbatim."""
        content = await PromptStore(self._database, conversation_id=message.chat.id).get(PromptType.CORE_MEMORY)
        body = f"⭐ Постоянная память\n\n{content}" if content else "⭐ Постоянная память пока пустая."
        await self._answer(message, body)

    async def show_schedules(self, message: Message) -> None:
        """`/schedules` — every armed reminder and routine, soonest first."""
        tasks = await ScheduleStore(self._database, conversation_id=message.chat.id).list()
        tasks.sort(key=lambda task: task.fire_at)
        rendered = "\n\n".join(_render_schedule(task) for task in tasks)
        await self._answer(message, rendered or "⏰ Ничего не запланировано.")

    async def tap(self, query: CallbackQuery, callback_data: SavedCallback) -> None:
        """Open an entry, go back to the index, or turn a page — all by editing the same message."""
        await query.answer()
        if not isinstance(query.message, Message):  # too old for Telegram to hand us the message
            return
        entries = await self._entries(callback_data.kind, query.message.chat.id)
        view = (
            _index_view(callback_data.kind, entries, callback_data.page)
            if callback_data.idx < 0
            else _entry_view(callback_data.kind, entries, callback_data.idx, callback_data.page)
        )
        chunks = split_message(view.text)
        try:
            await query.message.edit_text(chunks[0], reply_markup=view.markup)
        except TelegramBadRequest:  # re-tapping the open entry edits it to identical content
            logger.info(
                "Saved viewer tap changed nothing", extra={"kind": callback_data.kind, "idx": callback_data.idx}
            )
            return
        for extra in chunks[1:]:  # an entry longer than one Telegram message continues below it
            await query.message.answer(extra)

    async def _show_index(self, message: Message, kind: SavedKind) -> None:
        """Send the first page of a store's index."""
        entries = await self._entries(kind, message.chat.id)
        view = _index_view(kind, entries, page=0)
        await message.answer(view.text, reply_markup=view.markup)

    async def _entries(self, kind: SavedKind, conversation_id: int) -> list[_Entry]:
        """This chat's entries for one store, in the order the index and the buttons both use."""
        if kind is SavedKind.LISTS:
            lists = await ListStore(self._database, conversation_id=conversation_id).all()
            return [
                _Entry(label=_clip(f"📋 {item.name} ({len(item.items)})", _LABEL_LIMIT), body=_render_list(item))
                for item in sorted(lists, key=lambda item: item.name)
            ]
        memories = await MemoryStore(self._database, conversation_id=conversation_id).list()
        return [
            _Entry(label=_clip(f"🧠 {memory.title}", _LABEL_LIMIT), body=_render_memory(memory))
            for memory in sorted(memories, key=lambda memory: memory.updated_at, reverse=True)
        ]

    @staticmethod
    async def _answer(message: Message, text: str) -> None:
        """Reply with *text*, split across messages when it exceeds Telegram's per-message limit."""
        for chunk in split_message(text):
            await message.answer(chunk)
