"""SavedViewer: what the owner sees for each store, and that a tapped position opens what its caption promised.

The load-bearing invariant is the ordering: the stores return raw Mongo order, buttons carry a
POSITION, so the viewer's own sort is the only thing making a tap mean what the caption said. The
boundary test drives `SavedViewer` end to end (command -> keyboard -> tap) over a fake collection;
the rest unit-test the renderers and the paging arithmetic a boundary test would obscure.
"""

from aiogram import types
from baski.primitives import datetime

from app.chat.saved import (
    _PAGE_SIZE,
    SavedCallback,
    SavedKind,
    SavedViewer,
    _clip,
    _Entry,
    _fit_one_message,
    _Index,
    _render_list,
    _render_memory,
    _render_schedule,
)
from app.chat.format import MAX_MESSAGE_LENGTH
from app.lists.store import ItemList
from app.memory.store import Memory, MemoryCategory, MemorySource, SourceKind
from app.scheduling.store import ScheduledTask, ScheduleKind

CHAT_ID = 42


class _FakeCursor:
    """Async-iterable over the docs a find() matched, with the chained .sort() pymongo offers."""

    def __init__(self, docs: list[dict]) -> None:
        self._docs = docs

    def sort(self, key: str, direction: int) -> "_FakeCursor":
        return _FakeCursor(sorted(self._docs, key=lambda d: d[key], reverse=direction < 0))

    def __aiter__(self):  # noqa: ANN204
        return self._gen()

    async def _gen(self):  # noqa: ANN202
        for doc in self._docs:
            yield doc


class _FakeCollection:
    """The two read operations the viewer's stores perform: a filtered find() and find_one()."""

    def __init__(self, docs: list[dict]) -> None:
        self._docs = docs

    def _matching(self, flt: dict) -> list[dict]:
        return [d for d in self._docs if all(d.get(k) == v for k, v in flt.items())]

    def find(self, flt: dict) -> _FakeCursor:
        return _FakeCursor(self._matching(flt))

    async def find_one(self, flt: dict) -> dict | None:
        matches = self._matching(flt)
        return matches[0] if matches else None


class _FakeDatabase(dict):
    """`database[collection]` — the only way a store reaches Mongo."""

    def __missing__(self, name: str) -> _FakeCollection:
        return _FakeCollection([])


def _lists_db(*names_and_items: tuple[str, list[str]]) -> _FakeDatabase:
    docs = [
        {"conversation_id": CHAT_ID, "name": name, "items": items, "deleted_at": None}
        for name, items in names_and_items
    ]
    return _FakeDatabase(lists=_FakeCollection(docs))


def _message() -> types.Message:
    chat = types.Chat.model_construct(id=CHAT_ID, type="private")
    return types.Message.model_construct(message_id=1, chat=chat, text="/lists")


def _capture(monkeypatch) -> tuple[list, list]:  # noqa: ANN001 — pytest fixture
    """Record every send/edit as (kind, text, button captions), plus the keyboards themselves."""
    seen: list = []
    markups: list = []

    def _labels(markup) -> list[str]:  # noqa: ANN001 — aiogram InlineKeyboardMarkup | None
        return [] if markup is None else [b.text for row in markup.inline_keyboard for b in row]

    async def fake_answer(self, text, reply_markup=None, **kwargs):  # noqa: ANN001, ANN202
        seen.append(("send", text, _labels(reply_markup)))
        markups.append(reply_markup)
        return self

    async def fake_edit(self, text, reply_markup=None, **kwargs):  # noqa: ANN001, ANN202
        seen.append(("edit", text, _labels(reply_markup)))
        markups.append(reply_markup)
        return self

    async def fake_ack(self, *args, **kwargs):  # noqa: ANN001, ANN202
        return None

    monkeypatch.setattr(types.Message, "answer", fake_answer)
    monkeypatch.setattr(types.Message, "edit_text", fake_edit)
    monkeypatch.setattr(types.CallbackQuery, "answer", fake_ack)
    return seen, markups


def _entries(count: int) -> list[_Entry]:
    return [_Entry(label=f"метка {i}", body=f"тело {i}") for i in range(count)]


def _buttons(markup) -> list[str]:  # noqa: ANN001 — aiogram InlineKeyboardMarkup
    return [button.text for row in markup.inline_keyboard for button in row]


# ── boundary: command -> keyboard -> tap, over a fake store ──


async def test_tapping_a_button_opens_the_list_its_caption_named(monkeypatch) -> None:
    """Mongo hands lists back unsorted, so this is what proves a position means what the caption said."""
    seen, markups = _capture(monkeypatch)
    # Stored in an order that is NOT the displayed one: 'ягоды' would win position 0 unsorted.
    viewer = SavedViewer(_lists_db(("ягоды", ["малина"]), ("покупки", ["молоко", "хлеб"])))

    await viewer.show_lists(_message())

    assert seen[-1] == ("send", "📋 Списки — 2", ["📋 покупки (2)", "📋 ягоды (1)"])
    # Tap the FIRST button by replaying its own payload — the caption said "покупки".
    first = markups[-1].inline_keyboard[0][0]
    query = types.CallbackQuery.model_construct(id="1", message=_message(), data=first.callback_data)
    await viewer.tap(query, SavedCallback.unpack(first.callback_data))

    assert seen[-1] == ("edit", "📋 покупки — 2 шт.\n\n1. молоко\n2. хлеб", ["⬅️ Назад"])

    await viewer.tap(query, SavedCallback(kind=SavedKind.LISTS, idx=-1, page=0))
    assert seen[-1] == ("edit", "📋 Списки — 2", ["📋 покупки (2)", "📋 ягоды (1)"])


async def test_empty_store_says_so_and_offers_no_buttons(monkeypatch) -> None:
    seen, _ = _capture(monkeypatch)
    await SavedViewer(_FakeDatabase()).show_lists(_message())
    assert seen[-1] == ("send", "📋 Списков пока нет — попроси меня что-нибудь записать.", [])


async def test_nothing_scheduled_says_so(monkeypatch) -> None:
    seen, _ = _capture(monkeypatch)
    await SavedViewer(_FakeDatabase()).show_schedules(_message())
    assert seen[-1] == ("send", "⏰ Ничего не запланировано.", [])


async def test_core_memory_is_shown_verbatim(monkeypatch) -> None:
    seen, _ = _capture(monkeypatch)
    db = _FakeDatabase(prompts=_FakeCollection([]))
    await SavedViewer(db).show_core(_message())
    assert seen[-1] == ("send", "⭐ Постоянная память пока пустая.", [])


async def test_help_lists_every_published_command(monkeypatch) -> None:
    """/help is generated from BOT_COMMANDS, so it can't drift from Telegram's own menu."""
    seen, _ = _capture(monkeypatch)
    await SavedViewer(_FakeDatabase()).show_help(_message())
    text = seen[-1][1]
    assert "/lists — 📋 Списки" in text
    assert "/help — ❓ Что я умею" in text


# ── renderers ──


def test_list_renders_every_item_numbered() -> None:
    item_list = ItemList(conversation_id=1, name="покупки", items=["молоко", "хлеб"])
    assert _render_list(item_list) == "📋 покупки — 2 шт.\n\n1. молоко\n2. хлеб"


def test_memory_renders_title_meta_and_full_body() -> None:
    memory = Memory(
        conversation_id=1,
        title="Летит 3-го",
        category=MemoryCategory.EVENT,
        source=MemorySource(kind=SourceKind.USER),
        body="Рейс SU100, вылет в 10:40.",
        updated_at=datetime.as_utc(datetime.datetime(2026, 7, 14)),
    )
    assert _render_memory(memory) == (
        "🧠 Летит 3-го\nсобытие · 14.07.2026 UTC · с твоих слов\n\nРейс SU100, вылет в 10:40."
    )


def test_external_memory_shows_where_it_came_from() -> None:
    """The provenance link is the point of an unsummarized view — it must survive."""
    memory = Memory(
        conversation_id=1,
        title="Курс по GCP",
        category=MemoryCategory.FACT,
        source=MemorySource(kind=SourceKind.EXTERNAL, ref="https://cloud.google.com/learn"),
        body="Стартует в сентябре.",
        updated_at=datetime.as_utc(datetime.datetime(2026, 7, 14)),
    )
    assert _render_memory(memory) == (
        "🧠 Курс по GCP\nфакт · 14.07.2026 UTC · из внешнего источника\n"
        "🔗 https://cloud.google.com/learn\n\nСтартует в сентябре."
    )


def test_recurring_schedule_shows_period_and_utc() -> None:
    task = ScheduledTask(
        conversation_id=1,
        kind=ScheduleKind.RECURRING,
        instruction="Спроси про спорт",
        fire_at=datetime.as_utc(datetime.datetime(2026, 8, 2, 7, 30)),
        repeat_every_hours=24,
    )
    assert _render_schedule(task) == "⏰ 02.08.2026 07:30 UTC · каждые 24 ч\nСпроси про спорт"


def test_one_off_reminder_shows_no_period() -> None:
    """A one-shot has no repeat_every_hours — rendering it as a period would print 'каждые None ч'."""
    task = ScheduledTask(
        conversation_id=1,
        kind=ScheduleKind.ONCE,
        instruction="Позвонить в банк",
        fire_at=datetime.as_utc(datetime.datetime(2026, 8, 2, 7, 30)),
    )
    assert _render_schedule(task) == "⏰ 02.08.2026 07:30 UTC\nПозвонить в банк"


def test_long_entry_is_cut_to_one_message_and_says_it_was_cut() -> None:
    """An opened entry must stay one message so ⬅️ Назад can restore the index by editing it."""
    body = "\n".join(f"{n}. позиция {n}" for n in range(1, 900))
    fitted = _fit_one_message(body)
    assert len(fitted) <= MAX_MESSAGE_LENGTH
    assert fitted.endswith("… дальше не поместилось — запись длиннее одного сообщения Telegram")
    assert fitted.startswith("1. позиция 1\n2. позиция 2\n")  # whole lines, never a split number
    assert _fit_one_message("коротко") == "коротко"


def test_long_button_caption_is_clipped() -> None:
    assert _clip("к" * 60) == "к" * 39 + "…"
    assert _clip("коротко") == "коротко"


# ── index paging ──


def test_index_pages_and_buttons_carry_absolute_positions() -> None:
    entries = _entries(_PAGE_SIZE + 3)
    text, markup = _Index(SavedKind.MEMORY, entries).page(1)

    assert "стр. 2/2" in text and f"— {len(entries)}" in text
    assert _buttons(markup)[:3] == ["метка 8", "метка 9", "метка 10"]
    opened = SavedCallback.unpack(markup.inline_keyboard[0][0].callback_data)
    assert (opened.kind, opened.idx, opened.page) == (SavedKind.MEMORY, _PAGE_SIZE, 1)
    assert _buttons(markup)[-1] == "‹ Назад"  # last page: back only, no forward


def test_single_page_index_has_no_paging_row() -> None:
    text, markup = _Index(SavedKind.LISTS, _entries(2)).page(0)
    assert "стр." not in text
    assert _buttons(markup) == ["метка 0", "метка 1"]


def test_entry_opens_full_body_with_a_back_button() -> None:
    text, markup = _Index(SavedKind.MEMORY, _entries(3)).entry(idx=1, page=0)
    assert text == "тело 1"
    back = SavedCallback.unpack(markup.inline_keyboard[0][0].callback_data)
    assert (back.idx, back.page) == (-1, 0)


def test_tapping_an_entry_that_no_longer_exists_falls_back_to_the_index() -> None:
    """A memory forgotten between render and tap shifts positions — show the fresh index, not a wrong entry."""
    text, markup = _Index(SavedKind.MEMORY, _entries(2)).entry(idx=7, page=0)
    assert text == "🧠 Заметки — 2"
    assert _buttons(markup) == ["метка 0", "метка 1"]
