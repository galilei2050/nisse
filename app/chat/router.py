"""Telegram I/O router — text or voice message → Assistant.reply() → answer (voiced back if inbound was voice)."""

import logging
from io import BytesIO
from typing import TYPE_CHECKING

from aiogram import Bot, Router
from aiogram.enums import ChatAction
from aiogram.types import BufferedInputFile, Message, Voice
from baski.agents import AgentBillingError, AgentProviderUnavailableError, AgentRefusalError

from app.chat.progress import TelegramProgress
from app.chat.speak import Speaker
from app.chat.transcribe import Transcriber

if TYPE_CHECKING:  # injected at call time — importing it at runtime would cycle (chat → assistant → chat)
    from app.assistant import Assistant

logger = logging.getLogger(__name__)

_NON_TEXT_REPLY = "Send me a text or voice message."
_TRANSCRIBE_FAILED_REPLY = "I couldn't make out that voice message — please try again."
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


def build_router(*, assistant: "Assistant", transcriber: Transcriber, speaker: Speaker) -> Router:
    """Build the chat router whose handler delegates every text/voice message to the assistant."""
    router = Router(name="chat")

    @router.message()
    async def handle(message: Message, bot: Bot) -> None:
        """Resolve a text or voice message to text, delegate to the assistant, send back its reply."""
        text = await _resolve_text(message, bot, transcriber)
        if not text:  # non-text/voice, or empty transcript — already answered by _resolve_text
            return
        await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)
        progress = TelegramProgress(bot=bot, chat_id=message.chat.id)
        try:
            result = await assistant.reply(conversation_id=message.chat.id, text=text, on_event=progress)
            await progress.finish(result)
            if message.voice and result.response:  # voice in → voice out, alongside the text reply above
                await _voice_reply(message, bot, speaker, result.response)
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


async def _resolve_text(message: Message, bot: Bot, transcriber: Transcriber) -> str | None:
    """A text message's text, or a voice note's transcript; None (after answering) if neither."""
    if message.text:
        return message.text
    if message.voice:
        return await _transcribe_voice(message, message.voice, bot, transcriber)
    await message.answer(_NON_TEXT_REPLY)
    return None


async def _voice_reply(message: Message, bot: Bot, speaker: Speaker, text: str) -> None:
    """Voice the text answer back (voice-message turns only).

    Best-effort: the text reply is already delivered, so a TTS/adapt failure (any Anthropic or ElevenLabs
    error) logs and is dropped rather than turning a good answer into an error message.
    """
    await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.RECORD_VOICE)
    try:
        audio = await speaker.speak(text)
    except Exception:  # noqa: BLE001 — intentional degrade for a non-essential add-on; text answer already sent
        logger.warning("Voice reply failed; text answer already sent", exc_info=True)
        return
    await message.answer_voice(BufferedInputFile(audio, filename="reply.ogg"))


async def _transcribe_voice(message: Message, voice: Voice, bot: Bot, transcriber: Transcriber) -> str:
    """Download the voice note, transcribe it, and echo the transcript so a mis-hear is visible."""
    await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)
    buf = BytesIO()
    await bot.download(voice, destination=buf)
    try:
        text = await transcriber.transcribe(buf.getvalue())
    except Exception:
        await message.reply(_TRANSCRIBE_FAILED_REPLY)
        raise
    if not text:  # silence / too short to make out anything
        await message.reply(_TRANSCRIBE_FAILED_REPLY)
        return ""
    await message.reply(f"🎤 {text}")
    return text
