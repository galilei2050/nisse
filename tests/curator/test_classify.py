"""The classifier's parse: a malformed answer must fail loudly, not quietly become "nothing found".

A silently-empty classification looks exactly like a quiet night, so the curator would report "no
signal" on a day full of corrections and nobody would know the pass was broken.
"""

import pytest

from app.curator.classify import MessageKind, _parse


def test_a_fenced_json_block_parses() -> None:
    """Models fence JSON by habit; rejecting that would fail most healthy runs."""
    raw = '```json\n{"signals": [{"turn_id": 3, "kind": "correction", "quote": "я просил в рублях", "about": "currency"}]}\n```'

    parsed = _parse(raw)

    assert parsed.signals[0].kind is MessageKind.CORRECTION
    assert parsed.signals[0].turn_id == 3


def test_unusable_output_raises_instead_of_reading_as_a_quiet_night() -> None:
    with pytest.raises(ValueError, match="unusable output"):
        _parse("I looked at the conversation and found three corrections.")
