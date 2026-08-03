"""ask_user: what the AGENT receives back for each way the owner can answer.

The tool result is the model's only feedback channel here, so these drive the real `execute` and tap
the real keyboard payloads through the real state machine — only `query.answer()` and the keyboard
redraw (pure Telegram rendering) are skipped. Two of these pin contracts that broke silently before:
the multi-select answer must carry raw labels, never the `☐` checkbox glyph the buttons wear, and a
TYPED answer must reach the parked turn — otherwise it queues behind that turn's own chat lock.
"""

import asyncio
from typing import cast

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup

from app.chat.ask import AskUserTool, answer_pending, resolve_tap
from app.chat.reactions import ReactionRecorder
from app.chat.router import build_router
from app.chat.saved import SavedViewer
from tests.backend.test_reactions import _FakeDatabase

CHAT_ID = 42


class _FakeQuestion:
    """The sent question message — the tool deletes it once the question is settled."""

    def __init__(self) -> None:
        self.deleted = False

    async def delete(self) -> None:
        self.deleted = True


class _FakeBot:
    """Sends the question, then taps the buttons the test names — as the owner's thumb would."""

    def __init__(self, *taps: tuple[int, int]) -> None:
        self.taps = taps
        self.question = _FakeQuestion()
        self.sent = asyncio.Event()

    async def send_message(self, *, chat_id: int, text: str, reply_markup: InlineKeyboardMarkup) -> _FakeQuestion:
        self.chat_id, self.text = chat_id, text
        self.sent.set()
        for row, col in self.taps:
            data = reply_markup.inline_keyboard[row][col].callback_data
            assert data is not None
            resolve_tap(data)
        return self.question


def _tool(bot: _FakeBot) -> AskUserTool:
    return AskUserTool(bot=cast("Bot", bot), chat_id=CHAT_ID)


def test_the_router_subscribes_to_button_taps() -> None:
    """Telegram delivers callback_query only for a registered observer — without it every tap is
    dropped and the tool waits out its full timeout with no error anywhere."""
    router = build_router(
        assistant=object(),
        transcriber=object(),
        speaker=object(),
        saved=SavedViewer(_FakeDatabase()),
        reactions=ReactionRecorder(_FakeDatabase()),
    )
    assert "callback_query" in router.resolve_used_update_types()


async def test_a_single_choice_tap_returns_the_label_and_clears_the_question() -> None:
    bot = _FakeBot((1, 0))  # second option
    answer = await _tool(bot).execute(question="Во сколько?", options=["09:00", "12:00"])

    assert answer == "The user selected: 12:00"
    assert bot.question.deleted  # a spent keyboard left in the chat invites a stale tap


async def test_multi_select_waits_for_done_and_answers_with_raw_labels() -> None:
    """Multi buttons wear a `☐`/`✅` prefix; echoing the button TEXT would feed the model the glyph."""
    bot = _FakeBot((0, 0), (1, 0), (2, 0))  # pick two, then Done (first button of the tail row)
    answer = await _tool(bot).execute(question="Что купить?", options=["Молоко", "Хлеб"], multi=True)

    assert answer == "The user selected: Молоко, Хлеб"


async def test_done_with_nothing_picked_does_not_answer() -> None:
    """An empty confirm must keep waiting, or the agent gets 'The user selected: ' — an answer that
    says nothing, indistinguishable from a real choice."""
    bot = _FakeBot((2, 0), (2, 1))  # Done with no selection, then "None of these"
    answer = await _tool(bot).execute(question="Что купить?", options=["Молоко", "Хлеб"], multi=True)

    assert answer == "The user indicated none of the options fit."


async def test_none_of_these_tells_the_agent_the_options_missed() -> None:
    bot = _FakeBot((2, 0))  # the tail row's only button when single-choice
    answer = await _tool(bot).execute(question="Какой город?", options=["Москва", "Питер"])

    assert answer == "The user indicated none of the options fit."


async def test_a_typed_answer_reaches_the_parked_turn() -> None:
    """The owner types "в 9 утра" as readily as they tap. That message can't go through
    `assistant.reply` — the parked turn holds the chat's lock — so the router hands it here."""
    bot = _FakeBot()  # sends the question and taps nothing
    task = asyncio.create_task(_tool(bot).execute(question="Во сколько?", options=["09:00", "12:00"]))
    await bot.sent.wait()

    assert answer_pending(chat_id=CHAT_ID, text="в 9 утра")
    assert await task == "The user answered: в 9 утра"
    assert bot.question.deleted


async def test_a_typed_message_with_no_question_waiting_is_left_alone() -> None:
    """Every ordinary message passes through this check — swallowing one would silence the chat."""
    assert not answer_pending(chat_id=CHAT_ID, text="привет")
