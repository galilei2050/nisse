"""`/curate` runs the maintenance pass instead of being answered by the agent.

The catch-all message handler is registered last, so a command whose handler is missing is not an
error anywhere — it is silently paid for as an Opus turn that answers "разбери день" with words
instead of running the pass. That is what these drive the real router for.
"""

from types import SimpleNamespace
from typing import Any, cast

from aiogram import Bot

from app.chat.curate import CurateCommand
from app.chat.reactions import ReactionRecorder
from app.chat.router import ChatRouter
from app.chat.saved import BOT_COMMANDS, ChatCommand, SavedViewer
from tests.backend.test_ask import CHAT_ID, _FakeAssistant, _message, questions
from tests.backend.test_reactions import _FakeDatabase


class _FakeBot:
    """Records what was said to the owner: `message.answer` reaches a bot as a SendMessage call."""

    def __init__(self) -> None:
        self.texts: list[str] = []

    async def __call__(self, method: Any, **_kwargs: Any) -> bool:
        self.texts.append(getattr(method, "text", ""))
        return True

    async def send_chat_action(self, **_kwargs: Any) -> None:
        """The catch-all path types before it answers; a command must not get that far."""


class _FakeCurator:
    """Records the pass it was asked for; running one needs a live model and four live stores."""

    def __init__(self) -> None:
        self.curated: list[int] = []

    async def curate(self, *, conversation_id: int) -> Any:
        self.curated.append(conversation_id)
        return SimpleNamespace(changes=0)


def _router(assistant: _FakeAssistant, curator: _FakeCurator) -> Any:
    return ChatRouter(
        assistant=cast("Any", assistant),
        transcriber=cast("Any", object()),
        speaker=cast("Any", object()),
        questions=questions,
    ).build(
        saved=SavedViewer(_FakeDatabase()),
        curate=CurateCommand(cast("Any", curator)),
        reactions=ReactionRecorder(_FakeDatabase(), turns=cast("Any", None)),
    )


async def test_the_command_runs_the_pass_over_this_chat_and_never_reaches_the_agent() -> None:
    bot, assistant, curator = _FakeBot(), _FakeAssistant(), _FakeCurator()

    await _router(assistant, curator).propagate_event(
        "message", _message("/curate").as_(cast("Bot", bot)), bot=cast("Bot", bot)
    )

    assert curator.curated == [CHAT_ID]
    assert assistant.replies == 0  # the command was handled, not billed as a turn


async def test_the_owner_is_told_it_started_before_the_pass_blocks_for_minutes() -> None:
    """The pass takes minutes. Without this the chat sits silent, and the owner sends `/curate` again."""
    bot, assistant, curator = _FakeBot(), _FakeAssistant(), _FakeCurator()

    await _router(assistant, curator).propagate_event(
        "message", _message("/curate").as_(cast("Bot", bot)), bot=cast("Bot", bot)
    )

    assert any("Разбираю день" in text for text in bot.texts)


def test_the_command_is_in_the_published_menu() -> None:
    """The handler lives in another module than the menu entry; a command Telegram offers but nothing
    handles falls through to the catch-all and is answered — expensively — as a chat message."""
    assert any(command.command == ChatCommand.CURATE for command in BOT_COMMANDS)
