"""The classifier's parse: a malformed answer must fail loudly, not quietly become "nothing found".

A silently-empty classification looks exactly like a quiet night, so the curator would report "no
signal" on a day full of corrections and nobody would know the pass was broken.
"""

import pytest

from app.curator.classify import Classification, MessageKind, _parse


def test_a_fenced_json_block_parses() -> None:
    """Models fence JSON by habit; rejecting that would fail most healthy runs."""
    raw = '```json\n{"signals": [{"turn_id": 3, "kind": "correction", "quote": "я просил в рублях", "about": "currency"}]}\n```'

    parsed = _parse(raw)

    assert parsed.signals[0].kind is MessageKind.CORRECTION
    assert parsed.signals[0].turn_id == 3


def test_prose_instead_of_json_raises() -> None:
    with pytest.raises(ValueError, match="unusable output"):
        _parse("I looked at the conversation and found three corrections.")


def test_an_unknown_kind_raises_rather_than_being_dropped() -> None:
    """An invented label means the taxonomy and the prompt have drifted apart — that is a bug to see,
    not a signal to silently discard."""
    with pytest.raises(ValueError, match="unusable output"):
        _parse('{"signals": [{"turn_id": 1, "kind": "annoyed", "quote": "x", "about": "y"}]}')


def test_of_kind_selects_the_signals_the_curator_counts() -> None:
    """Recurrence across negative kinds is what promotes a lead into a standing rule."""
    parsed = _parse(
        '{"signals": ['
        '{"turn_id": 1, "kind": "request", "quote": "найди", "about": "flights"},'
        '{"turn_id": 2, "kind": "correction", "quote": "в рублях", "about": "currency"},'
        '{"turn_id": 3, "kind": "rejection", "quote": "не то", "about": "wrong hotel"}]}'
    )

    negative = parsed.of_kind(MessageKind.CORRECTION, MessageKind.REJECTION)

    assert [s.turn_id for s in negative] == [2, 3]
    assert isinstance(parsed, Classification)
