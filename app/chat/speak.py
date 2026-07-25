"""Text → voice — the TTS adapter: adapt the reply for speech (Haiku), then synthesize it (ElevenLabs)."""

import logging

from anthropic import AsyncAnthropic
from elevenlabs import AsyncElevenLabs

logger = logging.getLogger(__name__)

# Neutral female voice — ElevenLabs stock, present in this account. Change the voice by swapping this id;
# other neutral female voices already in the account: Matilda `XrExE9yKIg1WjnnlVkGX`, Alice `Xb7hH8MSUJpSbSDYk0k2`.
VOICE_ID = "EXAVITQu4vr4xnSDxMaL"  # Sarah — female, american, neutral

# Multilingual model so the voice speaks the reply's own language (RU/EN, auto) in its timbre.
TTS_MODEL = "eleven_multilingual_v2"
# Ogg/Opus @48kHz is exactly Telegram's voice-note format — the bytes go straight to answer_voice, no transcode.
TTS_FORMAT = "opus_48000_64"

# Cheap/fast pass that rewrites the markdown answer into something that reads naturally aloud.
_ADAPT_MODEL = "claude-haiku-4-5"
_ADAPT_MAX_TOKENS = 2048
_ADAPT_PROMPT = (
    "You rewrite an assistant's reply so it sounds natural read aloud by a text-to-speech voice. "
    "Keep the same language, meaning, and every fact. Drop markdown, emoji, and raw URLs. Turn tables, "
    "code blocks, and bullet lists into flowing spoken sentences (describe a table's contents in words). "
    "Expand symbols and abbreviations a voice would mangle. Output only the spoken text — no preamble."
)


class Speaker:
    """Voices an assistant reply: adapt-for-speech via Haiku, then TTS via ElevenLabs.

    Lifecycle: long-lived — one per process (built once in `backend.py`).
    """

    def __init__(self, *, elevenlabs: AsyncElevenLabs, anthropic: AsyncAnthropic) -> None:
        """Wrap the shared ElevenLabs (TTS) and Anthropic (speech-adaptation) clients."""
        self._elevenlabs = elevenlabs
        self._anthropic = anthropic

    async def speak(self, text: str) -> bytes:
        """Adapt `text` for speech, synthesize it, and return Telegram-ready Ogg/Opus bytes."""
        spoken = await self._adapt(text)
        return await self._synthesize(spoken)

    async def _adapt(self, text: str) -> str:
        """Rewrite the markdown reply into speech-friendly plain text via a cheap Haiku pass."""
        message = await self._anthropic.messages.create(
            model=_ADAPT_MODEL,
            max_tokens=_ADAPT_MAX_TOKENS,
            system=_ADAPT_PROMPT,
            messages=[{"role": "user", "content": text}],
        )
        spoken = "".join(block.text for block in message.content if block.type == "text").strip()
        logger.info("Reply adapted for speech", extra={"chars": len(spoken)})
        return spoken

    async def _synthesize(self, text: str) -> bytes:
        """ElevenLabs TTS → Ogg/Opus bytes (the format Telegram voice notes require)."""
        stream = self._elevenlabs.text_to_speech.convert(
            voice_id=VOICE_ID, text=text, model_id=TTS_MODEL, output_format=TTS_FORMAT
        )
        return b"".join([chunk async for chunk in stream])
