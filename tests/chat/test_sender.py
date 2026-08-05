"""What reaches the owner when a message is composed off the live-reply path.

The curator's report and a fired task's answer are written by the agent in ordinary markdown. Sent
raw they arrived as literal `**` and `##`, and anything past Telegram's limit was rejected whole.
"""

from typing import Any

from aiogram.exceptions import TelegramBadRequest

from app.chat.format import MAX_MESSAGE_LENGTH
from app.chat.sender import MarkdownSender


class _FakeBot:
    """Records every send, with the parse mode it was sent under."""

    def __init__(self, *, reject_markdown: bool = False) -> None:
        self.sent: list[tuple[str, str | None]] = []
        self._reject_markdown = reject_markdown

    async def send_message(self, *, chat_id: int, text: str, parse_mode: str | None) -> None:  # noqa: ARG002 — matches Bot.send_message
        if self._reject_markdown and parse_mode == "MarkdownV2":
            raise TelegramBadRequest(method=_UNUSED, message="can't parse entities")
        self.sent.append((text, parse_mode))


class _UnusedMethod:
    """TelegramBadRequest wants the failed call; nothing here reads it back."""


_UNUSED: Any = _UnusedMethod()


def _sender(bot: _FakeBot) -> MarkdownSender:
    return MarkdownSender(bot)  # type: ignore[arg-type]  # a fake stands in for Bot


async def test_markdown_arrives_as_formatting_not_as_characters() -> None:
    bot = _FakeBot()

    await _sender(bot).send(chat_id=1, text="**Что изменила**\n\nУбрала дубль.")

    (text, parse_mode) = bot.sent[0]
    assert parse_mode == "MarkdownV2"
    assert "**Что изменила**" not in text  # the literal asterisks the owner was seeing
    assert "*Что изменила*" in text  # MarkdownV2 bold


async def test_a_report_longer_than_one_message_is_split_instead_of_lost() -> None:
    """Telegram rejects an over-long message whole, so an unsplit report vanishes while its edits stand."""
    bot = _FakeBot()

    await _sender(bot).send(chat_id=1, text="\n\n".join(["Правка номер раз."] * 900))

    assert len(bot.sent) > 1
    assert all(len(text) <= MAX_MESSAGE_LENGTH for text, _ in bot.sent)


async def test_a_construct_telegram_refuses_is_delivered_as_plain_text() -> None:
    """A rejected chunk must still arrive: the pass already changed the owner's stores."""
    bot = _FakeBot(reject_markdown=True)

    await _sender(bot).send(chat_id=1, text="**Готово**")

    (text, parse_mode) = bot.sent[0]
    assert parse_mode is None
    assert text == "Готово"
