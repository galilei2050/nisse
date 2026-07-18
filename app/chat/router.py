"""Telegram I/O router — text message → Assistant.reply() → answer."""

from typing import TYPE_CHECKING

from aiogram import Bot, Router
from aiogram.enums import ChatAction
from aiogram.types import Message
from baski.agents import AgentBillingError, AgentProviderUnavailableError, AgentRefusalError

from app.chat.progress import TelegramProgress

if TYPE_CHECKING:  # injected at call time — importing it at runtime would cycle (chat → assistant → chat)
    from app.assistant import Assistant

_NON_TEXT_REPLY = "Send me a text message."
_REFUSAL_REPLY = "I couldn't answer that one — the model declined. Try rephrasing."
_ERROR_REPLY = "Something went wrong on my side — please try again."
_API_DOWN_REPLY = (
    "Anthropic's API is having problems right now, so I can't reply. Please try again shortly.\n"
    "https://status.claude.com/"
)
_BILLING_REPLY = (
    "💳 I'm out of Anthropic API credits, so I can't reply. Top up the balance in the Anthropic "
    "console (Plans & Billing) and try again.\n"
    "https://console.anthropic.com/settings/billing"
)


def build_router(*, assistant: "Assistant") -> Router:
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
            result = await assistant.reply(conversation_id=message.chat.id, text=message.text, on_event=progress)
            await progress.finish(result)
        except AgentRefusalError:
            await progress.finish_text(_REFUSAL_REPLY)
        except AgentProviderUnavailableError:
            await progress.finish_text(_API_DOWN_REPLY)
        except AgentBillingError:
            await progress.finish_text(_BILLING_REPLY)
        except Exception:
            await progress.finish_text(_ERROR_REPLY)
            raise
        finally:
            # History writes were fired during the reply; await them now the answer is delivered (on
            # every path), so Mongo latency never blocked the user but no completed turn is lost.
            await assistant.flush(conversation_id=message.chat.id)

    return router
