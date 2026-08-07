"""TelegramProgress: the chronological stream — every model text kept, tools humanized, verdicts inline."""

from types import SimpleNamespace

from baski.agents import (
    AgentExecuteResult,
    Completed,
    Judged,
    Message,
    TextDelta,
    ToolFinished,
    ToolStarted,
    TurnStarted,
)

from app.chat.progress import TelegramProgress

_LONG = "П" * 250  # a substantial chunk the model wrote between tool calls — must survive to the body


def _result(response: str) -> AgentExecuteResult:
    """A minimal result for finish(); footer reads total_cost + context_tokens."""
    return AgentExecuteResult(
        trace_id="t",
        response=response,
        total_input_tokens=0,
        total_output_tokens=0,
        turn_count=1,
        tool_call_count=0,
        total_cost=0.0123,
        context_tokens=12_400,
    )


class _FakeBot:
    """Records sends/edits so a test can read the message Telegram would have received."""

    def __init__(self) -> None:
        self.sends: list[str] = []
        self.edits: list[str] = []

    async def send_message(self, *, chat_id: int, text: str, parse_mode: str | None):
        self.sends.append(text)
        return SimpleNamespace(message_id=len(self.sends))  # distinct ids, as Telegram gives

    async def edit_message_text(self, *, text: str, chat_id: int, message_id: int, parse_mode: str | None):
        self.edits.append(text)


async def _drive(bot: _FakeBot, prog: TelegramProgress) -> str:
    """Short narration + tool, then long prose + tool, then the final answer. Returns the settled message."""
    await prog(TurnStarted(turn=1))
    await prog(Message(text="Смотрю, что там."))
    await prog(ToolStarted(name="google_search", tool_input={"query": "milk"}))
    await prog(ToolFinished(name="google_search", ok=True, duration_ms=300))

    await prog(TurnStarted(turn=2))
    await prog(Message(text=_LONG))
    await prog(ToolStarted(name="list_edit", tool_input={"name": "todo"}))
    await prog(ToolFinished(name="list_edit", ok=True, duration_ms=200))

    await prog(TurnStarted(turn=3))
    for ch in "Готово, финальный ответ.":
        await prog(TextDelta(text=ch))
    await prog(Completed(response="Готово, финальный ответ."))
    await prog.finish(_result("Готово, финальный ответ."))
    return bot.edits[-1]


async def test_nothing_the_model_said_is_dropped():
    bot = _FakeBot()
    final = await _drive(bot, TelegramProgress(bot=bot, chat_id=1))
    assert "Смотрю, что там" in final  # short narration kept as content
    assert _LONG in final  # long between-tools prose kept, not overwritten by the final answer
    assert "Готово, финальный ответ" in final  # and the final answer is there too


async def test_tools_render_humanized_raw_name_gone():
    bot = _FakeBot()
    final = await _drive(bot, TelegramProgress(bot=bot, chat_id=1))
    assert "Ищу" in final and "google_search" not in final  # search humanized
    assert "Список" in final and "list_edit" not in final  # list_edit humanized
    quoted = "\n".join(ln for ln in final.splitlines() if ln.startswith(">"))
    assert "Ищу" in quoted  # tool steps live in the blockquote
    assert "Смотрю, что там" not in quoted  # narration is body content, not a step line


async def test_judge_verdict_is_inline_after_its_text():
    bot = _FakeBot()
    prog = TelegramProgress(bot=bot, chat_id=1)
    await prog(TurnStarted(turn=1))
    for ch in "Черновик без цен.":
        await prog(TextDelta(text=ch))
    await prog(Judged(finished=False, missing=["цены"], feedback="Добавь цены.", attempt=1))
    for ch in "Финал с ценами.":
        await prog(TextDelta(text=ch))
    await prog(Completed(response="Финал с ценами."))
    await prog.finish(_result("Финал с ценами."))
    final = bot.edits[-1]
    assert "Черновик без цен" in final  # the graded draft is kept, not wiped
    assert "Добавь цены" in final  # the verdict's feedback is shown
    # chronology: graded text, then its verdict, then the redo
    assert final.index("Черновик") < final.index("Добавь цены") < final.index("Финал")


async def test_every_delivered_message_id_is_kept_when_the_answer_splits():
    """The owner can react to any chunk of a long answer; an id we never recorded is a reaction that
    can never be traced back to the turn it graded."""
    bot = _FakeBot()
    prog = TelegramProgress(bot=bot, chat_id=1)
    await prog(TurnStarted(turn=1))
    await prog(Message(text="Начинаю."))  # first send — the live message the rest edits
    await prog(Message(text="Длинный ответ. " * 500))  # >4096 chars → settles across several messages
    await prog.finish(_result("…"))

    assert len(prog.message_ids) > 1  # it really split, so the ids of the extra messages matter
    assert prog.message_ids == list(range(1, len(bot.sends) + 1))  # every send recorded, in order
    assert bot.edits  # the first message was edited in place, not re-sent


async def test_every_parallel_call_of_one_tool_gets_its_own_done_mark():
    """A batch is emitted as all starts, then all finishes (baski `_execute_tools`). Keyed by name
    alone, later starts overwrite earlier ones and the settled message shows most of the batch as
    still running — a permanent record telling the owner work was left unfinished."""
    bot = _FakeBot()
    prog = TelegramProgress(bot=bot, chat_id=1)
    queries = ["средний чек", "маржинальность", "тренды EV"]
    await prog(TurnStarted(turn=1))
    for query in queries:
        await prog(ToolStarted(name="google_search", tool_input={"query": query}))
    for ms in (1200, 2500, 3100):
        await prog(ToolFinished(name="google_search", ok=True, duration_ms=ms))
    await prog.finish(_result("Готово."))

    final = bot.edits[-1]
    assert final.count("✅") == len(queries)  # every call closed, not just the last one
    assert "🔍" not in final  # none left rendered as in-flight
    for query, seconds in zip(queries, ("1\\.2s", "2\\.5s", "3\\.1s"), strict=True):
        line = next(ln for ln in final.splitlines() if query in ln)
        assert seconds in line  # each line carries ITS call's duration, not the last one's


async def test_finish_appends_cost_footer():
    bot = _FakeBot()
    final = await _drive(bot, TelegramProgress(bot=bot, chat_id=1))
    assert "контекст" in final  # footer (cost + context size) appended on settle
