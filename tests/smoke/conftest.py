import os

import pytest_asyncio
from aiogram import Bot


@pytest_asyncio.fixture
async def bot():
    """Bot bound to the runner-provided TELEGRAM_TOKEN (same bot the process runs)."""
    bot = Bot(token=os.environ["TELEGRAM_TOKEN"])
    yield bot
    await bot.session.close()
