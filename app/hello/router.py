"""Catch-all Telegram router: every incoming message gets 'hello' back."""

from aiogram import Router
from aiogram.types import Message

router = Router(name="hello")


@router.message()
async def hello(message: Message) -> None:
    """Reply with a fixed greeting to any text message."""
    await message.answer("hello")
