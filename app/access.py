"""Allow-list middleware — restricts the bot to its owner, turns everyone else away."""

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware, types

__all__ = ["AllowlistMiddleware", "is_allowed"]

# Hardcoded owner. Telegram usernames are case-insensitive — compared lower-cased.
ALLOWED_USERNAMES = {"galilei"}

ACCESS_DENIED_MESSAGE = (
    "🔒 Nisse is a private assistant and isn't open to the public yet.\n"
    "If you'd like access, please reach out to @galilei."
)


def is_allowed(username: str | None) -> bool:
    """Return True if the Telegram username is on the allow-list (case-insensitive)."""
    return (username or "").lower() in ALLOWED_USERNAMES


class AllowlistMiddleware(BaseMiddleware):
    """Drops messages from anyone outside the allow-list, replying with a contact hint.

    Register as outer middleware on the message observer: `dp.message.outer_middleware(...)`.
    """

    async def __call__(
        self,
        handler: Callable[[types.TelegramObject, dict[str, Any]], Awaitable[Any]],  # noqa: ANON002 — aiogram middleware contract
        event: types.TelegramObject,
        data: dict[str, Any],  # noqa: ANON002 — aiogram middleware context dict
    ) -> Any:  # noqa: ANN401 — aiogram middleware/observer forwarding
        """Forward the event only if the sender's username is allow-listed."""
        if isinstance(event, types.Message):
            username = event.from_user.username if event.from_user else None
            if not is_allowed(username):
                await event.answer(ACCESS_DENIED_MESSAGE)
                return None
        return await handler(event, data)
