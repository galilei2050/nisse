"""When a nightly pass runs at all, and what the owner is told when it dies half-way through.

The curator's tools commit their edits as they run, so a pass that crashes mid-review can leave the
owner's stores already rewritten. Nothing re-runs it — Cloud Scheduler is configured with no retries
— so if the crash goes unreported, the owner wakes to changed behaviour and no explanation, which is
the one failure the whole attributed-and-reported design exists to prevent.

The other boundary is the cheap one: whether the window holds anything to learn from is a question
the turns query answers, so a night the owner slept through must not cost a review to discover.

These drive `Curator.curate` at its boundary with the review stubbed out, because a real review needs
a live Anthropic call and a healthy pass never takes this path.
"""

from types import SimpleNamespace
from typing import Any

import pytest
from baski.agents import AgentExecuteResult, Verdict
from baski.primitives import datetime

from app.chat.format import compose_answer
from app.curator.classify import Classification, MessageClassifier
from app.curator.curator import Curator
from app.scheduling.store import SCHEDULED_PREFIX

CONVERSATION = 42
AT = datetime.as_utc(datetime.datetime(2026, 8, 3, 9, 0))


class _FakeCursor:
    """Async-iterable over matched docs, with the chained `.sort()` pymongo offers."""

    def __init__(self, docs: list[dict]) -> None:
        self._docs = docs

    def sort(self, *_args: Any) -> "_FakeCursor":
        return self

    async def to_list(self, length: int | None = None) -> list[dict]:  # noqa: ARG002 — pymongo's signature
        return self._docs

    def __aiter__(self) -> Any:
        return self._iter()

    async def _iter(self) -> Any:
        for doc in self._docs:
            yield doc


class _FakeCollection:
    def __init__(self, docs: list[dict] | None = None) -> None:
        self.docs = docs or []
        self.inserted: list[dict] = []

    def find(self, *_args: Any, **_kwargs: Any) -> _FakeCursor:
        return _FakeCursor(self.docs)

    async def insert_one(self, doc: dict) -> SimpleNamespace:
        self.inserted.append(doc)
        return SimpleNamespace(inserted_id="000000000000000000000001")


class _FakeDatabase(dict):
    def __missing__(self, name: str) -> _FakeCollection:
        collection = _FakeCollection()
        self[name] = collection
        return collection


class _FakeSender:
    """Collects what the owner would have received; rendering is `tests/chat/test_sender.py`."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, *, chat_id: int, text: str) -> None:  # noqa: ARG002 — matches MessageSender
        self.sent.append(text)


def _turn(turn_id: int, owner: str, answer: str) -> dict:
    return {
        "conversation_id": CONVERSATION,
        "turn_id": turn_id,
        "created_at": AT,
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": owner}]},
            {"role": "assistant", "content": [{"type": "text", "text": answer}]},
        ],
    }


def _scheduled_turn(turn_id: int, instruction: str, answer: str) -> dict:
    """A reminder firing: it enters the transcript as a user message, but the owner never typed it."""
    return _turn(turn_id, f"{SCHEDULED_PREFIX} {instruction}", answer)


def _curator(database: _FakeDatabase, sender: _FakeSender) -> Curator:
    deps = SimpleNamespace(database=database, anthropic=SimpleNamespace())
    # The real formatter the backend supplies — what the owner receives is the thing under test here.
    return Curator(deps, sender=sender, format_report=compose_answer)  # type: ignore[arg-type]  # fake CoreDeps


def _result(report: str, *, cost: float) -> AgentExecuteResult:
    """A finished review, as baski's loop returns it: the report, what it cost, and the judge's verdict."""
    return AgentExecuteResult(
        trace_id="trace-1",
        response=report,
        total_input_tokens=1000,
        total_output_tokens=100,
        turn_count=3,
        tool_call_count=2,
        total_cost=cost,
        context_tokens=12_400,
        judge_verdicts=[Verdict(finished=True, missing=[], feedback="")],
    )


@pytest.fixture
def _reviewed_day() -> _FakeDatabase:
    database = _FakeDatabase()
    database["conversation_turns"] = _FakeCollection([_turn(1, "посчитай бюджет", "готово")])
    return database


@pytest.fixture(autouse=True)
def _no_classifier_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """Labelling the window is a live model call; these tests are about what happens after it."""

    async def _labelled(*_args: Any, **_kwargs: Any) -> Classification:
        return Classification(signals=[])

    monkeypatch.setattr(MessageClassifier, "classify", _labelled)


async def test_a_crashed_pass_is_recorded_and_reported_before_the_error_escapes(
    _reviewed_day: _FakeDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The edits are already committed when the review dies, so the owner must hear about it and the
    run must exist in the history — otherwise an overnight rewrite is indistinguishable from nothing
    having happened, and the error alone reaches only the logs.
    """
    sender = _FakeSender()
    curator = _curator(_reviewed_day, sender)

    async def _boom(*_args: Any, **_kwargs: Any) -> AgentExecuteResult:
        raise RuntimeError("judge unavailable")

    monkeypatch.setattr(Curator, "_review", _boom)

    with pytest.raises(RuntimeError, match="judge unavailable"):
        await curator.curate(conversation_id=CONVERSATION)

    (recorded,) = _reviewed_day["curator_runs"].inserted
    assert "Проход упал" in recorded["report"]
    assert recorded["run_id"] in recorded["report"]  # joins the owner's message to the trace in the logs
    (message,) = sender.sent
    assert "изменений: 0" in message  # the header carries the real count, so the body never claims one
    assert "Проход упал" in message


async def test_a_healthy_pass_reports_the_review_not_the_crash_text(
    _reviewed_day: _FakeDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The crash outcome is seeded before the review runs; if a success failed to overwrite it, every
    good night would tell the owner the pass had crashed.
    """
    sender = _FakeSender()
    curator = _curator(_reviewed_day, sender)

    async def _ok(*_args: Any, **_kwargs: Any) -> AgentExecuteResult:
        return _result("Убрала дубль в списке покупок.", cost=0.5)

    monkeypatch.setattr(Curator, "_review", _ok)

    run = await curator.curate(conversation_id=CONVERSATION)

    assert run.report == "Убрала дубль в списке покупок."
    assert run.cost == 0.5
    (message,) = sender.sent
    assert "Проход упал" not in message
    assert "Убрала дубль в списке покупок." in message


async def test_the_report_carries_the_verdict_and_the_cost_like_any_other_reply(
    _reviewed_day: _FakeDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A report the owner cannot price and cannot see was checked is one they have to audit by hand —
    the same two things every interactive reply ends with.
    """
    sender = _FakeSender()
    curator = _curator(_reviewed_day, sender)

    async def _ok(*_args: Any, **_kwargs: Any) -> AgentExecuteResult:
        return _result("Поправила правило про валюту.", cost=0.6651)

    monkeypatch.setattr(Curator, "_review", _ok)

    await curator.curate(conversation_id=CONVERSATION)

    (message,) = sender.sent
    assert "⚖️ ✅ готово" in message  # graded and passed — an inverted branch would read 🔄 here
    assert "$0.6651" in message
    assert "контекст 12.4k" in message


async def test_a_crashed_pass_says_so_without_inventing_a_cost(
    _reviewed_day: _FakeDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pass that died has no result to render — pricing or grading it would be reporting a run that
    never finished.
    """
    sender = _FakeSender()
    curator = _curator(_reviewed_day, sender)

    async def _boom(*_args: Any, **_kwargs: Any) -> AgentExecuteResult:
        raise RuntimeError("judge unavailable")

    monkeypatch.setattr(Curator, "_review", _boom)

    with pytest.raises(RuntimeError, match="judge unavailable"):
        await curator.curate(conversation_id=CONVERSATION)

    (recorded,) = _reviewed_day["curator_runs"].inserted
    assert recorded["cost"] == 0.0
    (message,) = sender.sent
    assert "⚖️" not in message
    assert "$" not in message


async def test_a_window_of_only_scheduled_check_ins_never_reaches_the_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A night the owner slept through is answerable from the turns query, so it must cost nothing.

    The exchanges are real and the assistant did answer — what is absent is the owner, and every
    lever the pass can pull needs the owner's words to justify it.
    """
    database = _FakeDatabase()
    database["conversation_turns"] = _FakeCollection(
        [_scheduled_turn(1, "Вечерний чек-ин (20:25 PT)", "20:25 — через пять минут комп в сон.")]
    )
    sender = _FakeSender()

    async def _must_not_run(*_args: Any, **_kwargs: Any) -> AgentExecuteResult:
        raise AssertionError("the review ran on a window with no owner input")

    monkeypatch.setattr(Curator, "_review", _must_not_run)

    run = await _curator(database, sender).curate(conversation_id=CONVERSATION)

    assert run.owner_messages == 0
    assert run.changes == 0
    assert run.cost == 0.0
    assert run.exchanges_reviewed == 1  # the window was not empty — the owner just was not in it
    assert sender.sent == []  # nothing happened, so there is nothing to report


async def test_a_reaction_with_no_message_still_earns_a_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    """An emoji on yesterday's answer is the owner's cheapest signal — skipping it would drop the
    one channel `/help` tells him to use when he has nothing to type.
    """
    database = _FakeDatabase()
    database["conversation_turns"] = _FakeCollection([_scheduled_turn(1, "Вечерний чек-ин", "как прошёл день?")])
    database["reactions"] = _FakeCollection(
        [{"conversation_id": CONVERSATION, "turn_id": 1, "current": ["👎"], "reacted_at": AT}]
    )
    sender = _FakeSender()

    async def _ok(*_args: Any, **_kwargs: Any) -> AgentExecuteResult:
        return _result("отчёт", cost=0.2)

    monkeypatch.setattr(Curator, "_review", _ok)

    run = await _curator(database, sender).curate(conversation_id=CONVERSATION)

    assert run.reactions_reviewed == 1
    assert run.report == "отчёт"
    assert len(sender.sent) == 1
