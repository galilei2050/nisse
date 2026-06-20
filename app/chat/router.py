"""Telegram I/O router — text message → Assistant.reply() → answer."""

from aiogram import Bot, Router
from aiogram.enums import ChatAction
from aiogram.types import Message
from baski.agents import AgentRefusalError

from app.assistant import Assistant
from app.chat.progress import TelegramProgress

_NON_TEXT_REPLY = "Send me a text message."
_REFUSAL_REPLY = "I couldn't answer that one — the model declined. Try rephrasing."
_ERROR_REPLY = "Something went wrong on my side — please try again."


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
        try:
            answer = await assistant.reply(conversation_id=message.chat.id, text=message.text, on_event=progress)
            await progress.finish(answer)
        except AgentRefusalError:
            await progress.finish(_REFUSAL_REPLY)
        except Exception:
            await progress.finish(_ERROR_REPLY)
            raise
        finally:
            # History writes were fired during the reply; await them now the answer is delivered (on
            # every path), so Mongo latency never blocked the user but no completed turn is lost.
            await assistant.flush(conversation_id=message.chat.id)

    return router
