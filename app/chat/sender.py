"""Sending a finished agent message to a chat, outside the live-reply path."""

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest

from .format import split_message, strip_markdown_v2, to_markdown_v2

__all__ = ["MarkdownSender"]

logger = logging.getLogger(__name__)


class MarkdownSender:
    """Delivers agent-written markdown to one chat. Lifecycle: long-lived, one per bot.

    The interactive reply renders through `TelegramProgress`, which edits a live message as the
    answer grows. A curator report and a scheduled task's answer have no live message to edit, and
    went out through a bare `send_message` — so `**bold**` and `##` arrived as literal characters
    and anything past 4096 UTF-16 units was rejected whole. Both now send through here.
    """

    def __init__(self, bot: Bot) -> None:
        """Hold the bot the messages go out on."""
        self._bot = bot

    async def send(self, *, chat_id: int, text: str) -> None:
        """Convert *text* to MarkdownV2, cut it to Telegram's message limit, and deliver each piece."""
        for chunk in split_message(to_markdown_v2(text)):
            await self._send_chunk(chat_id=chat_id, text=chunk)

    async def _send_chunk(self, *, chat_id: int, text: str) -> None:
        """Send one converted chunk, retrying as plain text when Telegram rejects the entities.

        Conversion is a real parser, but the agent writes free-form markdown and a construct
        Telegram refuses to parse would otherwise lose the whole message. Plain text delivers.
        """
        try:
            await self._bot.send_message(chat_id=chat_id, text=text, parse_mode="MarkdownV2")
        except TelegramBadRequest:
            logger.warning("Telegram rejected MarkdownV2; sending plain", exc_info=True)
            await self._bot.send_message(chat_id=chat_id, text=strip_markdown_v2(text), parse_mode=None)
