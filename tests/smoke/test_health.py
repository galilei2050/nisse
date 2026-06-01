"""Smoke — the bot is reachable on Telegram with the configured token.

Pairs with `make smoke-test`, which boots the bot in polling and waits for the
"Run polling for bot @…" health line before this runs.
"""


async def test_get_me(bot):
    me = await bot.get_me()

    assert me.is_bot
    assert me.username
