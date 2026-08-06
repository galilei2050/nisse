"""`/curate` — run the maintenance pass over this chat now, instead of waiting for the night.

The nightly pass is the normal way this runs (Cloud Scheduler → `POST /curate`). This command exists
because the owner sometimes wants the correction they just made to hold *tomorrow morning* rather
than after another day of the bot repeating it — and because a pass whose report they can trigger and
read on demand is a pass they can actually judge.

The pass is slow (minutes: it reads the day, then runs an Opus agent over four stores), so the
handler says it started and then blocks until the report goes out through the curator's own sender.
Blocking is safe here: in webhook mode the update is processed by a Cloud Task with a 30-minute
dispatch deadline (baski's `TelegramServer`), the same budget the nightly HTTP trigger runs under.
"""

import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from app.chat.saved import ChatCommand
from app.curator import Curator

logger = logging.getLogger(__name__)

_STARTED = (
    "🌙 Разбираю день: перечитаю переписку и реакции, поправлю память, списки, правила и промпты. "
    "Займёт несколько минут — отчёт пришлю сюда же."
)


class CurateCommand:
    """Runs the curator on demand from chat. Lifecycle: long-lived — one per bot."""

    def __init__(self, curator: Curator) -> None:
        """Hold the same curator the nightly HTTP trigger drives — one pass implementation, two callers."""
        self._curator = curator

    def register(self, router: Router) -> None:
        """Wire `/curate` onto the chat router, ahead of the catch-all that would bill it as an agent turn."""
        router.message.register(self.run, Command(ChatCommand.CURATE))

    async def run(self, message: Message) -> None:
        """Acknowledge, then run one pass over this chat; the curator sends the report itself."""
        await message.answer(_STARTED)
        logger.info("Curator pass requested from chat", extra={"conversationId": message.chat.id})
        await self._curator.curate(conversation_id=message.chat.id)
