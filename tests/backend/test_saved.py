"""SavedViewer rendering: what the owner actually sees for each store, and how paging behaves.

The point under test is that the viewer shows saved state verbatim (nothing summarized away) and
that its position-keyed buttons stay honest when the store changes between render and tap.
"""

from baski.primitives import datetime

from app.chat.saved import (
    _PAGE_SIZE,
    SavedCallback,
    SavedKind,
    _entry_view,
    _Entry,
    _index_view,
    _render_list,
    _render_memory,
    _render_schedule,
)
from app.lists.store import ItemList
from app.memory.store import Memory, MemoryCategory, MemorySource, SourceKind
from app.scheduling.store import ScheduledTask, ScheduleKind


def _entries(count: int) -> list[_Entry]:
    return [_Entry(label=f"метка {i}", body=f"тело {i}") for i in range(count)]


def _buttons(markup) -> list[str]:  # noqa: ANN001 — InlineKeyboardMarkup, aiogram type
    return [button.text for row in markup.inline_keyboard for button in row]


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
    assert _render_memory(memory) == "🧠 Летит 3-го\nсобытие · 14.07.2026 · с твоих слов\n\nРейс SU100, вылет в 10:40."


def test_recurring_schedule_shows_period_and_utc() -> None:
    task = ScheduledTask(
        conversation_id=1,
        kind=ScheduleKind.RECURRING,
        instruction="Спроси про спорт",
        fire_at=datetime.as_utc(datetime.datetime(2026, 8, 2, 7, 30)),
        repeat_every_hours=24,
    )
    assert _render_schedule(task) == "⏰ 02.08.2026 07:30 UTC · каждые 24 ч\nСпроси про спорт"


def test_empty_store_says_so_without_a_keyboard() -> None:
    text, markup = _index_view(SavedKind.LISTS, [], page=0)
    assert markup is None
    assert "пока нет" in text


def test_index_pages_and_buttons_carry_absolute_positions() -> None:
    entries = _entries(_PAGE_SIZE + 3)
    text, markup = _index_view(SavedKind.MEMORY, entries, page=1)

    assert "стр. 2/2" in text and f"— {len(entries)}" in text
    assert _buttons(markup)[:3] == ["метка 8", "метка 9", "метка 10"]
    opened = SavedCallback.unpack(markup.inline_keyboard[0][0].callback_data)
    assert (opened.kind, opened.idx, opened.page) == (SavedKind.MEMORY, _PAGE_SIZE, 1)
    assert _buttons(markup)[-1] == "‹ Назад"  # last page: back only, no forward


def test_single_page_index_has_no_paging_row() -> None:
    text, markup = _index_view(SavedKind.LISTS, _entries(2), page=0)
    assert "стр." not in text
    assert _buttons(markup) == ["метка 0", "метка 1"]


def test_entry_opens_full_body_with_a_back_button() -> None:
    text, markup = _entry_view(SavedKind.MEMORY, _entries(3), idx=1, page=0)
    assert text == "тело 1"
    back = SavedCallback.unpack(markup.inline_keyboard[0][0].callback_data)
    assert (back.idx, back.page) == (-1, 0)


def test_tapping_an_entry_that_no_longer_exists_falls_back_to_the_index() -> None:
    """A memory forgotten between render and tap shifts positions — show the fresh index, not a wrong entry."""
    text, markup = _entry_view(SavedKind.MEMORY, _entries(2), idx=7, page=0)
    assert "🧠 Заметки — 2" == text
    assert _buttons(markup) == ["метка 0", "метка 1"]
