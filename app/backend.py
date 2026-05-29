"""TelegramServer entry point — mounts every domain router on the dispatcher."""

import sys
from collections.abc import Iterable

from aiogram import Router
from baski.telegram.server import TelegramServer

from app import hello


class NisseBot(TelegramServer):
    """Personal-assistant Telegram bot. Polling locally, FastAPI webhook on --cloud."""

    def routers(self) -> Iterable[Router]:
        """Return every domain router to mount on the aiogram Dispatcher."""
        return [hello.router]


if __name__ == "__main__":
    sys.exit(NisseBot().run())
