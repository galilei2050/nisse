"""What a domain needs from the chat layer to deliver a message it produced."""

from typing import Protocol

__all__ = ["MessageSender"]


class MessageSender(Protocol):
    """Delivers one finished agent message to one chat.

    Domains take this rather than the bot itself. Both the curator's report and a scheduled task's
    answer are agent-written markdown that still needs converting and cutting to Telegram's limit,
    and `app.chat` already imports scheduling — so reaching for the chat layer directly would both
    couple a domain to the transport and cycle. `app.chat.sender.MarkdownSender` implements this.
    """

    async def send(self, *, chat_id: int, text: str) -> None:
        """Deliver *text* to *chat_id*, rendered the way the transport wants it."""
        ...
