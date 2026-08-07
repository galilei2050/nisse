"""Conversation: a message the owner sends mid-reply joins the run instead of starting a second one.

The agent here is a stand-in that does what baski's loop does to a history — build each turn's
payload from `format_for_api()` — because that is the seam the delivery rides on. What the tests
pin down is the boundary: delivered before the run's last turn → answered in the same run; delivered
after it → one extra pass, never dropped and never left waiting for the owner to write again.
"""

from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any, cast

from anthropic.types import MessageParam
from baski.agents import AgentExecuteResult, Verdict

from app.assistant.conversation import Conversation
from app.assistant.history import MongoMessageHistory

from .test_history import _FakeCollection, _FakeDatabase, _texts


_TURNS_PER_RUN = 2
_COST_PER_RUN = 1.0


def _result() -> AgentExecuteResult:
    return AgentExecuteResult(
        trace_id="t",
        response="готово",
        total_input_tokens=10,
        total_output_tokens=5,
        turn_count=_TURNS_PER_RUN,
        tool_call_count=1,
        total_cost=_COST_PER_RUN,
        context_tokens=10,
        judge_verdicts=[Verdict(finished=True, missing=[], feedback="ok")],
    )


class _FakeAgent:
    """Builds a payload off the real history for each turn, like baski's loop, then answers."""

    def __init__(self, history: MongoMessageHistory) -> None:
        self._history = history
        self.runs = 0
        self.payloads: list[list[MessageParam]] = []
        self.before_turn: Callable[[], Awaitable[None]] | None = None
        self.after_run: Callable[[], Awaitable[None]] | None = None

    async def execute(self) -> AgentExecuteResult:
        self.runs += 1
        for _ in range(_TURNS_PER_RUN):
            if self.before_turn is not None:
                await self.before_turn()
            self.payloads.append(self._history.format_for_api())
        if self.after_run is not None:
            await self.after_run()
        return _result()


def _conversation() -> tuple[Conversation, _FakeAgent, MongoMessageHistory]:
    history = MongoMessageHistory(database=cast("Any", _FakeDatabase(_FakeCollection())), conversation_id=1)
    agent = _FakeAgent(history)
    conversation = Conversation(
        agent=cast("Any", agent),
        history=history,
        short_term=cast("Any", SimpleNamespace(clear=lambda: None)),
    )
    return conversation, agent, history


async def test_a_message_sent_mid_reply_is_answered_by_the_run_already_going() -> None:
    """The whole point: it reaches the model on the next turn of the SAME run. Starting a second run
    would re-pay for a judge and a trace, and answer a second time over a transcript that by then
    already holds the first answer."""
    conversation, agent, _ = _conversation()
    delivered: list[bool] = []

    async def owner_types() -> None:
        if len(agent.payloads) == 1:  # the first turn is built and away; the owner adds to it
            delivered.append(conversation.deliver("в евро, не в долларах"))

    agent.before_turn = owner_types

    await conversation.reply(joinable=True, text="сколько стоит")

    assert delivered == [True]
    assert agent.runs == 1
    assert "в евро, не в долларах" not in _texts(cast("Any", agent.payloads[0]))  # not yet said when built
    assert "в евро, не в долларах" in _texts(cast("Any", agent.payloads[1]))


async def test_a_message_that_arrives_after_the_last_turn_still_gets_answered() -> None:
    """Delivered while the answer is being written or graded, it is too late for any turn of that run.
    Left there it would sit unanswered until the owner wrote again — so the loop runs once more."""
    conversation, agent, _ = _conversation()

    async def owner_types_late() -> None:
        agent.after_run = None  # only the first run is interrupted
        conversation.deliver("и добавь налог")

    agent.after_run = owner_types_late

    reply = await conversation.reply(joinable=True, text="сколько стоит")

    assert agent.runs == 2
    assert "и добавь налог" in _texts(cast("Any", agent.payloads[2]))  # first turn of the second run
    assert reply.result.total_cost == 2.0  # both passes, under one answer the owner saw grow
    assert len(reply.result.judge_verdicts) == 2


async def test_a_scheduled_run_does_not_take_the_owner_s_messages() -> None:
    """A fired task drives this same conversation with nothing on the owner's screen. Folded into it,
    their message would be answered inside the task's own message, under a bare 👀 — so it is refused
    and the router starts a visible turn for it instead."""
    conversation, agent, _ = _conversation()
    accepted: list[bool] = []

    async def owner_types() -> None:
        if len(agent.payloads) == 1:
            accepted.append(conversation.deliver("а во сколько встреча?"))

    agent.before_turn = owner_types

    await conversation.reply(joinable=False, text="[Запланировано] утренний брифинг")

    assert accepted == [False]
    assert agent.runs == 1


async def test_a_message_is_not_delivered_when_no_reply_is_running() -> None:
    """The router falls back to starting a turn on False. Accepting one here would park it in the
    history with nothing running to read it — the bot would go silent."""
    conversation, agent, _ = _conversation()

    assert conversation.deliver("привет") is False
    assert agent.runs == 0


async def test_the_answered_turn_is_the_one_the_reply_reports() -> None:
    """The router links the sent Telegram messages against this id; taking the newest turn instead
    stamps them on whatever a later reply has since added."""
    conversation, _, history = _conversation()

    reply = await conversation.reply(joinable=True, text="вопрос")

    assert reply.turn_id == history.last_turn_id
