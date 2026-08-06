"""What the owner is told when a nightly pass dies half-way through.

The curator's tools commit their edits as they run, so a pass that crashes mid-review can leave the
owner's stores already rewritten. Nothing re-runs it — Cloud Scheduler is configured with no retries
— so if the crash goes unreported, the owner wakes to changed behaviour and no explanation, which is
the one failure the whole attributed-and-reported design exists to prevent.

These drive `Curator.curate` at its boundary with the review stubbed out, because a real review needs
a live Anthropic call and a healthy pass never takes this path.
"""

from types import SimpleNamespace
from typing import Any

import pytest
from baski.primitives import datetime

from app.curator.classify import Classification, MessageClassifier
from app.curator.curator import Curator, ReviewOutcome

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


def _curator(database: _FakeDatabase, sender: _FakeSender) -> Curator:
    deps = SimpleNamespace(database=database, anthropic=SimpleNamespace())
    return Curator(deps, sender=sender)  # type: ignore[arg-type]  # a fake stands in for CoreDeps


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
    having happened, and the error alone reaches only the logs."""
    sender = _FakeSender()
    curator = _curator(_reviewed_day, sender)

    async def _boom(*_args: Any, **_kwargs: Any) -> ReviewOutcome:
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
    good night would tell the owner the pass had crashed."""
    sender = _FakeSender()
    curator = _curator(_reviewed_day, sender)

    async def _ok(*_args: Any, **_kwargs: Any) -> ReviewOutcome:
        return ReviewOutcome(report="Убрала дубль в списке покупок.", cost=0.5)

    monkeypatch.setattr(Curator, "_review", _ok)

    run = await curator.curate(conversation_id=CONVERSATION)

    assert run.report == "Убрала дубль в списке покупок."
    assert run.cost == 0.5
    (message,) = sender.sent
    assert "Проход упал" not in message
    assert "Убрала дубль в списке покупок." in message
