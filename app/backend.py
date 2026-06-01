"""TelegramServer entry point — mounts every domain router on the dispatcher."""

import sys
from collections.abc import Iterable
from typing import Any

from aiogram import Router
from baski.telegram.server import TelegramServer

from app import hello
from app.access import AllowlistMiddleware


class NisseBot(TelegramServer):
    """Personal-assistant Telegram bot. Polling locally, FastAPI webhook on --cloud."""

    def routers(self) -> Iterable[Router]:
        """Return every domain router to mount on the aiogram Dispatcher."""
        return [hello.router]

    def outer_middlewares(self) -> Iterable[Any]:
        """Gate every message through the owner allow-list before any handler runs."""
        return [AllowlistMiddleware()]


if __name__ == "__main__":
    sys.exit(NisseBot().run())
