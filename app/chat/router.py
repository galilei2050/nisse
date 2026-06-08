"""Telegram I/O router — text message → Assistant.reply() → answer."""

from aiogram import Bot, Router
from aiogram.enums import ChatAction
from aiogram.types import Message

from app.assistant import Assistant
from app.chat.progress import TelegramProgress

_NON_TEXT_REPLY = "Send me a text message."


def build_router(*, assistant: Assistant) -> Router:
    """Build the chat router whose handler delegates every text message to the assistant."""
    router = Router(name="chat")

    @router.message()
    async def handle(message: Message, bot: Bot) -> None:
        """Delegate a text message to the assistant and send back its reply."""
        if not message.text:
            await message.answer(_NON_TEXT_REPLY)
            return
        await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)
        progress = TelegramProgress(bot=bot, chat_id=message.chat.id)
        answer = await assistant.reply(text=message.text, on_event=progress)
        await progress.finish(answer)

    return router
