"""Telegram I/O router — text or voice message → Assistant.reply() → answer (voiced back if inbound was voice)."""

import base64
import logging
from io import BytesIO
from typing import TYPE_CHECKING, NamedTuple

from aiogram import Bot, Router
from aiogram.enums import ChatAction
from aiogram.types import Audio, BufferedInputFile, Document, Message, PhotoSize, Voice
from baski.agents import AgentBillingError, AgentProviderUnavailableError, AgentRefusalError

from app.chat.ask import PendingQuestions
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


class ChatRouter:
    """The bot's Telegram surface: resolve an inbound message, run one agent turn, deliver the reply.

    Lifecycle: long-lived — one per process, built in `backend.py`.
    """

    def __init__(
        self, *, assistant: "Assistant", transcriber: Transcriber, speaker: Speaker, questions: PendingQuestions
    ) -> None:
        """Hold what one turn needs: the agent, the two voice adapters, and the parked-question registry."""
        self._assistant = assistant
        self._transcriber = transcriber
        self._speaker = speaker
        self._questions = questions

    def build(self, *, saved: SavedViewer, reactions: ReactionRecorder) -> Router:
        """Assemble the aiogram router: every other handler first, then this class's catch-all.

        Order is load-bearing for `saved` — aiogram tries handlers in registration order, so a
        catch-all registered ahead of the commands would swallow `/lists` into a paid agent turn.
        """
        router = Router(name="chat")
        self._questions.register(router)  # ask_user taps arrive as callback_query, not as messages
        saved.register(router)  # /lists /memory /core /schedules /help
        reactions.register(router)  # message_reaction updates — a different observer, order irrelevant
        router.message.register(self.handle)
        return router

    async def handle(self, message: Message, bot: Bot) -> None:
        """Resolve a text/voice/audio/photo/document message, delegate to the assistant, send back its reply."""
        resolved = await self._resolve(message, bot)
        if resolved is None:  # unsupported or empty — already answered by `_resolve`
            return
        # Only plain text can BE an answer — a photo's caption is a prompt for the image beside it,
        # and consuming it here would drop the image and hand the question an empty answer.
        answerable = resolved.media is None and bool(resolved.text.strip())
        if answerable and self._questions.answer(chat_id=message.chat.id, text=resolved.text):
            return  # the text WAS the answer; a new turn would queue behind the parked turn's chat lock
        await self._run_turn(message, bot, resolved)

    async def _run_turn(self, message: Message, bot: Bot, resolved: _Resolved) -> None:
        """Drive one agent turn behind a live progress message, rendering every failure the agent can raise."""
        await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)
        progress = TelegramProgress(bot=bot, chat_id=message.chat.id)
        try:
            result = await self._assistant.reply(
                conversation_id=message.chat.id, text=resolved.text, media=resolved.media, on_event=progress
            )
            await progress.finish(result)
            if message.voice and result.response:  # voice in → voice out, alongside the text reply above
                await self._voice_reply(message, bot, result.response)
        except AgentRefusalError:
            await progress.finish_text(_REFUSAL_REPLY)
        except AgentProviderUnavailableError:
            await progress.finish_text(_API_DOWN_REPLY)
        except AgentBillingError:
            await progress.finish_text(_BILLING_REPLY)
        except Exception:
            await progress.finish_text(_ERROR_REPLY)  # tell the owner, then crash loud into the logs
            raise
        finally:
            # History writes were fired during the reply; await them now the answer is delivered (on
            # every path), so Mongo latency never blocked the user but no completed turn is lost.
            await self._assistant.flush(conversation_id=message.chat.id)
            # Then tie the sent messages to that turn — only now do both exist, and a reaction arriving
            # days later has no other way back to what it graded.
            await self._assistant.link_messages(conversation_id=message.chat.id, message_ids=progress.message_ids)

    async def _resolve(self, message: Message, bot: Bot) -> _Resolved | None:
        """Resolve any inbound message to text + optional media; None (after answering) if unsupported/empty."""
        if message.text:
            return _Resolved(message.text, None)
        audio = message.voice or message.audio  # a voice note or an audio file sent as a file → transcribe
        if audio:
            text = await self._transcribe(message, audio, bot)
            return _Resolved(text, None) if text else None
        if message.photo:
            data = await self._download_b64(bot, message.photo[-1])  # last = highest resolution
            return _Resolved(message.caption or "", Media(data=data, media_type=MediaType.JPEG))
        if message.document:
            return await self._resolve_document(message, message.document, bot)
        await message.answer(_NON_TEXT_REPLY)
        return None

    async def _resolve_document(self, message: Message, document: Document, bot: Bot) -> _Resolved | None:
        """A document → an image or PDF the model can read; else decline (after answering)."""
        if document.file_size and document.file_size > _MAX_FILE_BYTES:
            await message.reply(_FILE_TOO_LARGE_REPLY)
            return None
        try:
            media_type = MediaType(document.mime_type or "")
        except ValueError:  # not an image/PDF the model reads
            await message.reply(_UNSUPPORTED_FILE_REPLY)
            return None
        data = await self._download_b64(bot, document)
        return _Resolved(message.caption or "", Media(data=data, media_type=media_type))

    async def _transcribe(self, message: Message, audio: Voice | Audio, bot: Bot) -> str:
        """Download a voice note or audio file, transcribe it, and echo the transcript so a mis-hear is visible."""
        await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)
        buf = BytesIO()
        await bot.download(audio, destination=buf)
        try:
            text = await self._transcriber.transcribe(buf.getvalue())
        except Exception:
            await message.reply(_TRANSCRIBE_FAILED_REPLY)  # tell the owner, then crash loud into the logs
            raise
        if not text:  # silence / too short to make out anything
            await message.reply(_TRANSCRIBE_FAILED_REPLY)
            return ""
        await message.reply(f"🎤 {text}")
        return text

    async def _voice_reply(self, message: Message, bot: Bot, text: str) -> None:
        """Voice the text answer back (voice-message turns only).

        Best-effort: the text reply is already delivered, so any failure in this add-on —
        Anthropic/ElevenLabs synthesis OR the Telegram send — logs and is dropped rather than turning
        a good answer into an error message.
        """
        try:
            await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.RECORD_VOICE)
            audio = await self._speaker.speak(text)
            await message.answer_voice(BufferedInputFile(audio, filename="reply.ogg"))
        except Exception:  # noqa: BLE001 — intentional degrade for a non-essential add-on; text answer already sent
            logger.warning("Voice reply failed; text answer already sent", exc_info=True)

    @staticmethod
    async def _download_b64(bot: Bot, file: PhotoSize | Document) -> str:
        """Download a Telegram file and base64-encode it for an Anthropic image/document block."""
        buf = BytesIO()
        await bot.download(file, destination=buf)
        return base64.standard_b64encode(buf.getvalue()).decode()
