"""Telegram I/O router — text or voice message → Assistant.reply() → answer (voiced back if inbound was voice)."""

import base64
import logging
from io import BytesIO
from typing import TYPE_CHECKING, NamedTuple

from aiogram import Bot, Router
from aiogram.enums import ChatAction
from aiogram.types import Audio, BufferedInputFile, Document, Message, PhotoSize, Voice
from baski.agents import AgentBillingError, AgentProviderUnavailableError, AgentRefusalError

from app.chat.ask import answer_pending, register_ask_handler
from app.chat.progress import TelegramProgress
from app.chat.reactions import ReactionRecorder
from app.chat.saved import SavedViewer
from app.chat.speak import Speaker
from app.chat.transcribe import Transcriber
from app.shared.blocks import Media, MediaType

if TYPE_CHECKING:  # injected at call time — importing it at runtime would cycle (chat → assistant → chat)
    from app.assistant import Assistant

logger = logging.getLogger(__name__)


class _Resolved(NamedTuple):
    """A resolved inbound message: the text (caption/transcript, maybe empty) and any photo/PDF."""

    text: str
    media: Media | None


_NON_TEXT_REPLY = "Send me text, a voice message, an audio file, a photo, or a PDF."
_UNSUPPORTED_FILE_REPLY = "I can open images and PDFs — that file type I can't read yet."
_FILE_TOO_LARGE_REPLY = "That file is too large for me to open (limit 20 MB)."
_MAX_FILE_BYTES = 20 * 1024 * 1024  # Telegram's own getFile/download ceiling
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


def build_router(  # noqa: PLR0913 — one collaborator per surface the chat router owns
    *,
    assistant: "Assistant",
    transcriber: Transcriber,
    speaker: Speaker,
    saved: SavedViewer,
    reactions: ReactionRecorder,
) -> Router:
    """Build the chat router whose handler delegates every text/voice message to the assistant."""
    router = Router(name="chat")
    register_ask_handler(router)  # resolves ask_user button taps (callback_query, not a message)
    saved.register(router)  # /lists /memory /core /schedules — registered before the catch-all below
    reactions.register(router)  # message_reaction updates — a different observer, order irrelevant

    @router.message()
    async def handle(message: Message, bot: Bot) -> None:
        """Resolve a text/voice/audio/photo/document message, delegate to the assistant, send back its reply."""
        resolved = await _resolve_message(message, bot, transcriber)
        if resolved is None:  # unsupported or empty — already answered by _resolve_message
            return
        # Only plain text can BE an answer — a photo's caption is a prompt for the image beside it,
        # and consuming it here would drop the image and hand the question an empty answer.
        answerable = resolved.media is None and bool(resolved.text.strip())
        if answerable and answer_pending(chat_id=message.chat.id, text=resolved.text):
            return  # the text WAS the answer; a new turn would queue behind the parked turn's chat lock
        await _run_turn(message, bot, assistant, speaker, resolved)

    return router


async def _run_turn(  # noqa: PLR0913 — the handler's collaborators, passed straight through
    message: Message, bot: Bot, assistant: "Assistant", speaker: Speaker, resolved: _Resolved
) -> None:
    """Drive one agent turn behind a live progress message, rendering every failure the agent can raise."""
    await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)
    progress = TelegramProgress(bot=bot, chat_id=message.chat.id)
    try:
        result = await assistant.reply(
            conversation_id=message.chat.id, text=resolved.text, media=resolved.media, on_event=progress
        )
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
        # Then tie the sent messages to that turn — only now do both exist, and a reaction arriving
        # days later has no other way back to what it graded.
        await assistant.link_messages(conversation_id=message.chat.id, message_ids=progress.message_ids)


async def _resolve_message(message: Message, bot: Bot, transcriber: Transcriber) -> _Resolved | None:
    """Resolve any inbound message to text + optional media; None (after answering) if unsupported/empty."""
    if message.text:
        return _Resolved(message.text, None)
    audio = message.voice or message.audio  # a voice note or an audio file sent as a file → transcribe
    if audio:
        text = await _transcribe_audio(message, audio, bot, transcriber)
        return _Resolved(text, None) if text else None
    if message.photo:
        return await _resolve_photo(message, message.photo[-1], bot)  # last = highest resolution
    if message.document:
        return await _resolve_document(message, message.document, bot)
    await message.answer(_NON_TEXT_REPLY)
    return None


async def _resolve_photo(message: Message, photo: PhotoSize, bot: Bot) -> _Resolved:
    """A photo → a JPEG image (Telegram always sends photos as JPEG); the caption is the prompt."""
    data = await _download_b64(bot, photo)
    return _Resolved(message.caption or "", Media(data=data, media_type=MediaType.JPEG))


async def _resolve_document(message: Message, document: Document, bot: Bot) -> _Resolved | None:
    """A document → an image or PDF the model can read; else decline (after answering)."""
    if document.file_size and document.file_size > _MAX_FILE_BYTES:
        await message.reply(_FILE_TOO_LARGE_REPLY)
        return None
    try:
        media_type = MediaType(document.mime_type or "")
    except ValueError:  # not an image/PDF the model reads
        await message.reply(_UNSUPPORTED_FILE_REPLY)
        return None
    data = await _download_b64(bot, document)
    return _Resolved(message.caption or "", Media(data=data, media_type=media_type))


async def _download_b64(bot: Bot, file: PhotoSize | Document) -> str:
    """Download a Telegram file and base64-encode it for an Anthropic image/document block."""
    buf = BytesIO()
    await bot.download(file, destination=buf)
    return base64.standard_b64encode(buf.getvalue()).decode()


async def _voice_reply(message: Message, bot: Bot, speaker: Speaker, text: str) -> None:
    """Voice the text answer back (voice-message turns only).

    Best-effort: the text reply is already delivered, so any failure in this add-on — Anthropic/ElevenLabs
    synthesis OR the Telegram send — logs and is dropped rather than turning a good answer into an error message.
    """
    try:
        await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.RECORD_VOICE)
        audio = await speaker.speak(text)
        await message.answer_voice(BufferedInputFile(audio, filename="reply.ogg"))
    except Exception:  # noqa: BLE001 — intentional degrade for a non-essential add-on; text answer already sent
        logger.warning("Voice reply failed; text answer already sent", exc_info=True)


async def _transcribe_audio(message: Message, audio: Voice | Audio, bot: Bot, transcriber: Transcriber) -> str:
    """Download a voice note or audio file, transcribe it, and echo the transcript so a mis-hear is visible."""
    await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)
    buf = BytesIO()
    await bot.download(audio, destination=buf)
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
