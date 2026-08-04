"""ask_user: what the AGENT receives back for each way the owner can answer, and who gets the message.

The tool result is the model's only feedback channel here, so these drive the real `execute` and the
real keyboard payloads. Two contracts nothing else enforces: a multi-select answer carries raw labels,
never the `☐` glyph the buttons wear, and a TYPED answer settles the question instead of starting a
second turn — which would queue behind the chat lock the parked turn is holding.
"""

import asyncio
from collections.abc import Iterator
from typing import Any, cast

import pytest
from aiogram import Bot
from aiogram.types import CallbackQuery, Chat, InlineKeyboardMarkup, Message, User
from baski.agents import AgentRefusalError
from baski.primitives import datetime

from app.chat import ask
from app.chat.ask import AskUserTool, answer_pending, resolve_tap
from app.chat.reactions import ReactionRecorder
from app.chat.router import build_router
from app.chat.saved import SavedViewer
from tests.backend.test_reactions import _FakeDatabase

CHAT_ID = 42
SENT_AT = datetime.as_utc(datetime.datetime(2026, 8, 2, 19, 0))


@pytest.fixture(autouse=True)
def _no_leftover_questions() -> Iterator[None]:
    """`_pending` is a module global; a question leaked by one test would answer another's message."""
    yield
    ask._pending.clear()


class _FakeQuestion:
    """The sent question message — the tool deletes it once the question is settled."""

    message_id = 99  # progress messages are edited by id, so the real send returns one

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
        self.texts: list[str] = []
        self.methods: list[str] = []
        self.keyboard: InlineKeyboardMarkup | None = None

    async def send_message(self, *, chat_id: int, text: str, **kwargs: Any) -> _FakeQuestion:
        self.chat_id = chat_id
        self.texts.append(text)
        self.keyboard = kwargs.get("reply_markup")
        self.sent.set()
        for row, col in self.taps:
            data = cast("InlineKeyboardMarkup", self.keyboard).inline_keyboard[row][col].callback_data
            assert data is not None
            resolve_tap(data)
        return self.question

    async def send_chat_action(self, **kwargs: Any) -> None:
        self.typing = True

    async def __call__(self, method: Any, **kwargs: Any) -> bool:
        """Aiogram calls the bot with a method object — that's how `query.answer()` reaches Telegram."""
        self.methods.append(type(method).__name__)
        return True


class _FakeAssistant:
    """Refuses every turn — the cheapest reply path that still proves the turn was started."""

    def __init__(self) -> None:
        self.replies = 0

    async def reply(self, **kwargs: Any) -> None:
        self.replies += 1
        raise AgentRefusalError("not today")

    async def flush(self, **kwargs: Any) -> None:
        pass

    async def link_messages(self, **kwargs: Any) -> None:
        pass


def _tool(bot: _FakeBot) -> AskUserTool:
    return AskUserTool(bot=cast("Bot", bot), chat_id=CHAT_ID)


def _router(assistant: _FakeAssistant) -> Any:
    return build_router(
        assistant=cast("Any", assistant),
        transcriber=object(),
        speaker=object(),
        saved=SavedViewer(_FakeDatabase()),
        reactions=ReactionRecorder(_FakeDatabase(), turns=cast("Any", None)),
    )


def _message(text: str) -> Message:
    return Message.model_construct(
        message_id=7,
        chat=Chat.model_construct(id=CHAT_ID, type="private"),
        from_user=User.model_construct(id=7, is_bot=False, first_name="V"),
        date=SENT_AT,
        text=text,
    )


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


async def test_an_unanswered_question_never_defaults_on_the_owner_s_behalf(monkeypatch: pytest.MonkeyPatch) -> None:
    """Telling the model to "proceed with a sensible default" here is how an invented value reaches
    production wearing the owner's authority — the whole failure this tool exists to prevent."""
    monkeypatch.setattr(ask, "_ANSWER_TIMEOUT", 0.01)
    bot = _FakeBot()  # sends the question and taps nothing

    answer = await _tool(bot).execute(question="Во сколько?", options=["09:00", "12:00"])

    assert answer == "The user did not answer. Do NOT invent a value for them — say what you still need."
    assert bot.question.deleted


async def test_a_second_question_in_the_same_chat_is_refused() -> None:
    """baski runs a turn's tool calls concurrently, so the model CAN open two questions at once —
    and a typed answer, routed by chat, would then land on whichever was asked first."""
    bot = _FakeBot()
    first = asyncio.create_task(_tool(bot).execute(question="Бюджет?", options=["до 5к", "5–20к"]))
    await bot.sent.wait()

    assert await _tool(bot).execute(question="Дата?", options=["сегодня", "завтра"]) == (
        "A question of yours is still unanswered — wait for it before asking another."
    )
    assert answer_pending(chat_id=CHAT_ID, text="до 5к")
    assert await asyncio.wait_for(first, timeout=1.0) == "The user answered: до 5к"


async def test_a_failed_send_leaves_no_question_behind() -> None:
    """A leaked entry matches by chat forever, so the owner's next message would resolve a question
    nobody is waiting on and never reach the agent at all."""

    class _BrokenBot(_FakeBot):
        async def send_message(self, **kwargs: Any) -> _FakeQuestion:
            raise ConnectionError("flood wait")

    with pytest.raises(ConnectionError):
        await _tool(_BrokenBot()).execute(question="Во сколько?", options=["09:00", "12:00"])

    assert not answer_pending(chat_id=CHAT_ID, text="в 9 утра")


async def test_a_typed_message_settles_the_question_instead_of_starting_a_turn() -> None:
    """The owner types "в 9 утра" as readily as they tap. Routing that through `assistant.reply`
    would deadlock — the parked turn holds the chat's lock — so the router must intercept it."""
    bot, assistant = _FakeBot(), _FakeAssistant()
    router = _router(assistant)
    task = asyncio.create_task(_tool(bot).execute(question="Во сколько?", options=["09:00", "12:00"]))
    await bot.sent.wait()

    await router.propagate_event("message", _message("в 9 утра"), bot=cast("Bot", bot))

    # Bounded: if the gate goes, the question is never settled and this waits out the real timeout.
    assert await asyncio.wait_for(task, timeout=1.0) == "The user answered: в 9 утра"
    assert assistant.replies == 0  # the message WAS the answer, not a new request


async def test_a_button_tap_reaches_the_parked_turn_through_the_router() -> None:
    """Taps arrive as callback_query. If the handler isn't registered on the router, Telegram's
    allowed_updates loses that type and every tap is silently dropped — the tool just times out."""
    bot, assistant = _FakeBot(), _FakeAssistant()
    router = _router(assistant)
    task = asyncio.create_task(_tool(bot).execute(question="Во сколько?", options=["09:00", "12:00"]))
    await bot.sent.wait()
    payload = cast("InlineKeyboardMarkup", bot.keyboard).inline_keyboard[0][0].callback_data

    query = CallbackQuery.model_construct(
        id="1",
        from_user=User.model_construct(id=7, is_bot=False, first_name="V"),
        chat_instance="ci",
        data=payload,
        message=_message("Во сколько?").as_(cast("Bot", bot)),
    ).as_(cast("Bot", bot))
    await router.propagate_event("callback_query", query, bot=cast("Bot", bot))

    assert await asyncio.wait_for(task, timeout=1.0) == "The user selected: 09:00"
    assert "AnswerCallbackQuery" in bot.methods  # the spinner on the owner's button was cleared


async def test_an_ordinary_message_still_starts_a_turn() -> None:
    """The gate sits in front of every message; if it ever swallowed one with no question waiting,
    the bot would go silent with no error to notice."""
    bot, assistant = _FakeBot(), _FakeAssistant()

    await _router(assistant).propagate_event("message", _message("привет"), bot=cast("Bot", bot))

    assert assistant.replies == 1
    assert any("model declined" in text for text in bot.texts)  # the refusal reached the owner (MarkdownV2-escaped)
