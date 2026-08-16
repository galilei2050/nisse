"""`transcript_read`: the pass's one look past the window it was handed.

The failure it exists for is not a crash — it is a permanent judge rule written on a complaint whose
other half had already aged out (`BUGS.md` #18). So what matters here is that the pass can reach the
disputed exchange, and that a model-supplied `limit` cannot turn one look backwards into a re-read of
the days the previous passes already acted on.
"""

from dataclasses import dataclass

from app.curator.evidence import Exchange
from app.curator.tools import _MAX_LIMIT, TranscriptReadTool

from baski.primitives import datetime

CONVERSATION = 42


@dataclass
class _FakeEvidence:
    """Records what the tool asked for; folding turns into exchanges is `tests/curator/test_evidence.py`."""

    exchanges: list[Exchange]
    asked: dict | None = None

    async def before(self, *, conversation_id: int, turn_id: int, limit: int) -> list[Exchange]:
        self.asked = {"conversation_id": conversation_id, "turn_id": turn_id, "limit": limit}
        return self.exchanges


def _exchange(turn_id: int, owner: str, answer: str) -> Exchange:
    return Exchange(
        turn_id=turn_id,
        at=datetime.as_utc(datetime.datetime(2026, 8, 15, 3, 26)),
        owner_text=owner,
        answer_text=answer,
    )


def _tool(evidence: _FakeEvidence) -> TranscriptReadTool:
    return TranscriptReadTool(evidence, conversation_id=CONVERSATION)  # type: ignore[arg-type]  # fake collector


async def test_it_renders_the_disputed_exchange_the_way_the_digest_does() -> None:
    """The pass reasons over one shape of text. A second rendering here would be a second thing to
    keep in step with the digest, and the owner's own words are what a complaint is checked against."""
    evidence = _FakeEvidence([_exchange(2491, "Дебил, я в поезде в Йосемити.", "Понял, чек-ин был не к месту")])

    result = await _tool(evidence).execute(before_turn_id=2497)

    assert "Turn 2491" in result
    assert "Дебил, я в поезде в Йосемити." in result  # the words the complaint the next day denies
    assert evidence.asked == {"conversation_id": CONVERSATION, "turn_id": 2497, "limit": 5}


async def test_an_oversized_limit_is_clipped_rather_than_refused() -> None:
    """A refusal would cost a turn and teach nothing; the cap is about not pulling a week of
    transcript into a pass whose whole budget is one day."""
    evidence = _FakeEvidence([])

    await _tool(evidence).execute(before_turn_id=2497, limit=500)

    assert evidence.asked is not None
    assert evidence.asked["limit"] == _MAX_LIMIT


async def test_a_nonsense_limit_still_reads_one_exchange() -> None:
    """`limit=0` would come back empty and read as "there is nothing older" — the opposite of true,
    and the pass would then write its rule on the complaint alone."""
    evidence = _FakeEvidence([])

    await _tool(evidence).execute(before_turn_id=2497, limit=0)

    assert evidence.asked is not None
    assert evidence.asked["limit"] == 1


async def test_an_exchange_cut_by_the_read_boundary_says_so() -> None:
    """An exchange spans several turns, so a fixed read lands mid-exchange about half the time and
    the owner's opening message stays behind the cut. Rendered silently that reads as "(no text)" —
    the owner said nothing — which is the exact misreading this whole tool exists to prevent."""
    cut = Exchange(
        turn_id=2496,
        at=datetime.as_utc(datetime.datetime(2026, 8, 15, 3, 27)),
        owner_text="",
        answer_text="Понял, чек-ин был не к месту.",
    )

    result = await _tool(_FakeEvidence([cut])).execute(before_turn_id=2497)

    assert "2496 is cut off" in result
    assert "read further back" in result


async def test_a_scheduled_prompt_is_not_reported_as_cut_off() -> None:
    """A check-in firing legitimately has no owner message — flagging it would send the pass walking
    back after words that were never there."""
    fired = Exchange(
        turn_id=2488,
        at=datetime.as_utc(datetime.datetime(2026, 8, 15, 3, 25)),
        owner_text="[Запланировано] Вечерний чек-ин",
        answer_text="20:25, пятница",
        scheduled=True,
    )

    result = await _tool(_FakeEvidence([fired])).execute(before_turn_id=2497)

    assert "cut off" not in result


async def test_reaching_past_the_start_says_so_instead_of_returning_nothing() -> None:
    """An empty string reads as a tool that failed. The pass needs to know it reached the beginning,
    so it stops walking back instead of retrying with a bigger limit."""
    result = await _tool(_FakeEvidence([])).execute(before_turn_id=1)

    assert "start of this conversation" in result
