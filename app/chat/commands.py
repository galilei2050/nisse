"""The bot's command menu: every published name, its description, and publishing it to Telegram.

One list, so `/` autocomplete, the menu button and `/help` can never disagree. Handlers are NOT here
— each command is registered by the module that owns its behaviour (`saved.py` for the store views,
`curate.py` for the maintenance pass). Names live together because the invariant "every published
name resolves to exactly one handler" belongs to whoever assembles the router, not to any one
handler: a name published with no handler falls through to the catch-all and is answered as a paid
agent turn.
"""

import logging
from enum import StrEnum

from aiogram import Bot, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BotCommand
from baski.pattern import retry

logger = logging.getLogger(__name__)


class ChatCommand(StrEnum):
    """The bot's command names — the handler filters and the published menu both read these.

    A name spelled in only one of the two would be offered by Telegram's autocomplete, match no
    handler, and reach the agent as a message. (Distinct from `SavedKind`, which is a callback
    payload: only the two stores that have an index view are valid there.)
    """

    LISTS = "lists"
    MEMORY = "memory"
    CORE = "core"
    SCHEDULES = "schedules"
    CURATE = "curate"
    HELP = "help"


BOT_COMMANDS = [
    BotCommand(command=ChatCommand.LISTS, description="📋 Списки"),
    BotCommand(command=ChatCommand.MEMORY, description="🧠 Заметки — что бот запомнил"),
    BotCommand(command=ChatCommand.CORE, description="⭐ Постоянная память"),
    BotCommand(command=ChatCommand.SCHEDULES, description="⏰ Напоминания и рутины"),
    BotCommand(command=ChatCommand.CURATE, description="🌙 Разобрать день сейчас"),
    BotCommand(command=ChatCommand.HELP, description="❓ Что я умею"),
]


async def publish_commands(bot: Bot) -> None:  # noqa: ANON003 — aiogram injects the bot into a startup hook
    """Publish the menu — `/` autocomplete and the chat's menu button read it.

    Cosmetic, so a Telegram outage must not take the bot down with it: in webhook mode startup runs
    inside the FastAPI lifespan, and raising here would leave Cloud Run without a ready revision over
    a menu nobody needs to get an answer.
    """
    try:
        await retry(bot.set_my_commands, exceptions=(TelegramAPIError,), commands=BOT_COMMANDS)
    except TelegramAPIError as exc:
        logger.warning("Command menu not published; the commands still work", extra={"error": str(exc)})


def register_menu(router: Router) -> None:
    """Publish the menu when the router starts."""
    router.startup.register(publish_commands)
