"""TelegramProgress: intermediate prose is kept (not overwritten), steps tuck under a cut."""

from types import SimpleNamespace

from baski.agents import Completed, Message, TextDelta, ToolFinished, ToolStarted, TurnStarted

from app.chat.progress import TelegramProgress

_LONG = "П" * 250  # over _NARRATION_MAX — substantial content the model wrote before a tool call


class _FakeBot:
    """Records sends/edits so a test can read the message Telegram would have received."""

    def __init__(self) -> None:
        self.sends: list[str] = []
        self.edits: list[str] = []

    async def send_message(self, *, chat_id: int, text: str, parse_mode: str | None):
        self.sends.append(text)
        return SimpleNamespace(message_id=1)

    async def edit_message_text(self, *, text: str, chat_id: int, message_id: int, parse_mode: str | None):
        self.edits.append(text)


async def _drive(bot: _FakeBot, prog: TelegramProgress) -> str:
    """Run a 3-turn scenario (short narration + tool, long prose + tool, final) and return the message."""
    await prog(TurnStarted(turn=1))
    await prog(Message(text="Смотрю, что там."))  # short -> step log
    await prog(ToolStarted(name="google_search", tool_input={"query": "milk"}))
    await prog(ToolFinished(name="google_search", ok=True, duration_ms=300))

    await prog(TurnStarted(turn=2))
    await prog(Message(text=_LONG))  # long -> kept as content
    await prog(ToolStarted(name="list_edit", tool_input={"name": "todo"}))
    await prog(ToolFinished(name="list_edit", ok=True, duration_ms=200))

    await prog(TurnStarted(turn=3))
    for ch in "Готово, финальный ответ.":
        await prog(TextDelta(text=ch))
    await prog(Completed(response="Готово, финальный ответ."))
    await prog.finish("Готово, финальный ответ.")
    return bot.edits[-1]  # the in-place settled message


async def test_substantial_prose_survives_to_final_message():
    bot = _FakeBot()
    final = await _drive(bot, TelegramProgress(bot=bot, chat_id=1))
    # The long intermediate text used to be discarded by finish(); now it is kept as content.
    assert _LONG in final
    assert "Готово, финальный ответ" in final  # and the final answer is still there


async def test_short_narration_and_tools_go_into_the_step_blockquote():
    bot = _FakeBot()
    final = await _drive(bot, TelegramProgress(bot=bot, chat_id=1))
    quote_lines = [ln for ln in final.splitlines() if ln.startswith(">") or ln.startswith("**>")]
    quoted = "\n".join(quote_lines)
    assert "Смотрю, что там" in quoted  # short narration is a status line, not body content
    assert "Ищу" in quoted and "google_search" not in final  # tool label humanized, raw name gone
    # The kept prose lives in the body, not inside the quote.
    assert _LONG not in quoted


async def test_long_step_log_is_expandable():
    bot = _FakeBot()
    prog = TelegramProgress(bot=bot, chat_id=1)
    for i in range(12):  # enough tool lines to push the blockquote over telegramify's 200-char cut
        await prog(ToolStarted(name="google_search", tool_input={"query": f"молоко хлеб яйца {i}"}))
        await prog(ToolFinished(name="google_search", ok=True, duration_ms=300))
    await prog.finish("Готово.")
    quote = bot.edits[-1].split("\n\n", 1)[0]
    # telegramify upgrades a >200-char blockquote to Telegram's expandable form: **> … ||.
    assert quote.startswith("**>") and quote.endswith("||")
