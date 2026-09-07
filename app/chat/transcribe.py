"""Voice → text — the STT adapter (ElevenLabs Scribe v2; provider-swappable)."""

import logging

from elevenlabs import AsyncElevenLabs, SpeechToTextChunkResponseModel

logger = logging.getLogger(__name__)

_MODEL = "scribe_v2"


class Transcriber:
    """Transcribes a Telegram voice note (OGG/Opus bytes) to text via ElevenLabs Scribe v2.

    Lifecycle: long-lived — one per process (built once in `backend.py`).
    """

    def __init__(self, *, client: AsyncElevenLabs) -> None:
        """Wrap the shared async ElevenLabs client."""
        self._client = client

    async def transcribe(self, audio: bytes) -> str:
        """Transcribe voice bytes to text. Language is auto-detected, so RU and EN both work.

        `no_verbatim` drops filler words / false starts (scribe_v2 only) so the agent gets clean intent.
        """
        result = await self._client.speech_to_text.convert(
            model_id=_MODEL,
            file=("voice.ogg", audio, "audio/ogg"),
            no_verbatim=True,
        )
        # Single-channel, non-webhook request → always the chunk model (the only variant with .text).
        if not isinstance(result, SpeechToTextChunkResponseModel):
            raise TypeError(f"Unexpected STT response: {type(result).__name__}")
        text = result.text.strip()
        logger.info("Voice transcribed", extra={"chars": len(text)})
        return text
