"""`transcript_read`: the pass's one look past the window it was handed.

The failure it exists for is not a crash — it is a permanent judge rule written on a complaint whose
other half had already aged out (`BUGS.md` #18). So these drive the real `EvidenceCollector` over a
fake Mongo: a stubbed collector would let "walks back and finds the disputed turn" pass without any
query having run, which is the one claim worth testing here.
"""

from baski.primitives import datetime

from app.curator.evidence import EvidenceCollector
from app.curator.tools import TranscriptReadTool
from tests.curator.fakes import FakeDatabase

CONVERSATION = 42


def _turn(turn_id: int, owner: str, answer: str, *, minute: int = 0) -> dict:
    return {
        "conversation_id": CONVERSATION,
        "turn_id": turn_id,
        "created_at": datetime.as_utc(datetime.datetime(2026, 8, 15, 3, minute)),
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": owner}]},
            {"role": "assistant", "content": [{"type": "text", "text": answer}]},
        ],
    }


def _tool(*, turns: list[dict], reactions: list[dict] | None = None) -> TranscriptReadTool:
    database = FakeDatabase(turns=turns, reactions=reactions or [])
    return TranscriptReadTool(EvidenceCollector(database), conversation_id=CONVERSATION)  # type: ignore[arg-type]  # fake db


async def test_it_reaches_the_disputed_turn_the_window_left_out() -> None:
    """The case from `BUGS.md` #18, in miniature: the digest starts at 2497, the owner's own words
    are at 2491, and the pass judged the complaint without them. The whole rendering is asserted —
    the answer line and the reaction are what a complaint is checked against, and a substring match
    would sleep through either going missing."""
    tool = _tool(
        turns=[
            _turn(2491, "Дебил, я в поезде в Йосемити.", "Понял, чек-ин был не к месту.", minute=26),
            _turn(2497, "Очень тупо добавлять поездку в ядро", "", minute=55),
        ],
        reactions=[
            {
                "conversation_id": CONVERSATION,
                "turn_id": 2491,
                "current": ["👎"],
                "reacted_at": datetime.as_utc(datetime.datetime(2026, 8, 15, 4, 0)),
            }
        ],
    )

    result = await tool.execute(before_turn_id=2497)

    assert result == (
        "[Turn 2491 · 2026-08-15 03:26 UTC]  reaction: 👎\n"
        "owner: Дебил, я в поезде в Йосемити.\n"
        "nisse: Понял, чек-ин был не к месту."
    )


def _tool_round(turn_id: int) -> dict:
    """A turn that is pure machinery — the shape a long question leaves between its words."""
    return {
        "conversation_id": CONVERSATION,
        "turn_id": turn_id,
        "created_at": datetime.as_utc(datetime.datetime(2026, 8, 15, 3, 30)),
        "messages": [{"role": "assistant", "content": [{"type": "tool_use", "id": f"t{turn_id}", "name": "search"}]}],
    }


async def test_a_read_that_lands_inside_one_question_says_where_to_resume() -> None:
    """Turns are not exchanges: a read can land entirely in the tool rounds of a question that opens
    earlier, and group to nothing. Answering "there is nothing here" would stop the walk one call
    short of the exchange it was sent to find."""
    opener = _turn(2400, "разбери мой год по месяцам", "")
    tool = _tool(turns=[opener, *(_tool_round(n) for n in range(2401, 2421))])

    result = await tool.execute(before_turn_id=2421)

    assert "2411" in result  # the oldest turn it actually read, so the pass knows where to resume
    assert "start of this conversation" not in result
    assert "tool rounds" in result


async def test_reaching_past_the_start_says_so_instead_of_returning_nothing() -> None:
    """An empty answer reads as a tool that failed. The pass needs to know it reached the beginning,
    so it stops walking back instead of calling again with a lower id forever."""
    result = await _tool(turns=[_turn(1, "привет", "привет")]).execute(before_turn_id=1)

    assert "start of this conversation" in result


async def test_an_exchange_cut_by_the_read_boundary_says_so() -> None:
    """A fixed read lands mid-exchange about as often as not, leaving the owner's opening message
    behind the cut. Rendered silently that reads as "(no text)" — the owner said nothing — the exact
    misreading this tool exists to prevent."""
    def _narration(turn_id: int) -> dict:
        return {
            "conversation_id": CONVERSATION,
            "turn_id": turn_id,
            "created_at": datetime.as_utc(datetime.datetime(2026, 8, 15, 3, 26)),
            "messages": [{"role": "assistant", "content": [{"type": "text", "text": f"шаг {turn_id}"}]}],
        }

    # The opener sits further back than the read reaches, so the exchange arrives without its words.
    opener = _turn(2400, "расскажи про Йосемити", "")
    tool = _tool(turns=[opener, *(_narration(n) for n in range(2401, 2421))])

    result = await tool.execute(before_turn_id=2421)

    assert "2411 is cut off" in result
    assert "read further back" in result


async def test_a_caption_less_photo_is_not_reported_as_cut_off() -> None:
    """The owner opened this exchange with a photo — `app/CLAUDE.md`: with no caption it IS the ask.
    Flagging it would send the pass hunting backwards for words that were never typed."""
    photo = {
        "conversation_id": CONVERSATION,
        "turn_id": 2491,
        "created_at": datetime.as_utc(datetime.datetime(2026, 8, 15, 3, 26)),
        "messages": [
            {"role": "user", "content": [{"type": "image", "source": {"type": "base64", "data": "…"}}]},
            {"role": "assistant", "content": [{"type": "text", "text": "Это чек на $42."}]},
        ],
    }

    result = await _tool(turns=[photo]).execute(before_turn_id=2492)

    assert result == (
        "[Turn 2491 · 2026-08-15 03:26 UTC]\nowner: (sent a photo or PDF, no caption)\nnisse: Это чек на $42."
    )


async def test_a_scheduled_prompt_is_not_reported_as_cut_off() -> None:
    """A check-in firing legitimately has no owner message. Flagged, it would send the pass walking
    back after words that never existed."""
    fired = _turn(2488, "[Запланировано] Вечерний чек-ин", "20:25, пятница", minute=25)

    result = await _tool(turns=[fired]).execute(before_turn_id=2489)

    assert result == (
        "[Turn 2488 · 2026-08-15 03:25 UTC]  (scheduled self-prompt — NOT the owner)\n"
        "schedule: [Запланировано] Вечерний чек-ин\n"
        "nisse: 20:25, пятница"
    )
