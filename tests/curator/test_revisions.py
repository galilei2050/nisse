"""Change history: a curator edit is attributed and the text it destroyed survives.

These assert the property the whole unattended-curator design rests on — that an edit made while the
owner slept can be read back and undone. A revision that lost `before`, or that was filed under the
wrong actor, would leave a change the owner can see happened but cannot reverse.
"""

from types import SimpleNamespace

from app.shared.revisions import Actor, ChangeKind, RevisionLog, acting_as, current_attribution

CONVERSATION = 42


class _FakeCollection:
    def __init__(self) -> None:
        self.inserted: list[dict] = []

    async def insert_one(self, doc: dict) -> SimpleNamespace:
        self.inserted.append(doc)
        return SimpleNamespace(inserted_id="000000000000000000000001")


class _FakeDatabase(dict):
    def __missing__(self, name: str) -> _FakeCollection:
        collection = _FakeCollection()
        self[name] = collection
        return collection


def _log(db: _FakeDatabase) -> RevisionLog:
    return RevisionLog(db, conversation_id=CONVERSATION)


async def test_a_curator_edit_keeps_the_text_it_replaced() -> None:
    """`before` is the only surviving copy of an overwritten core-memory block — lose it and the
    owner can see that the rule changed but never what it used to say."""
    db = _FakeDatabase()
    with acting_as(Actor.CURATOR, run_id="run7"):
        await _log(db).record(
            collection="prompts",
            target="core_memory",
            kind=ChangeKind.REPLACE,
            before="be concise",
            after="be concise\nanswer in Russian",
        )

    (doc,) = db["revisions"].inserted
    assert doc["before"] == "be concise"
    assert doc["after"] == "be concise\nanswer in Russian"
    assert doc["actor"] == Actor.CURATOR
    assert doc["run_id"] == "run7"
    assert doc["kind"] == ChangeKind.REPLACE


async def test_attribution_is_restored_after_the_run_block() -> None:
    """A curator pass and a live reply run in the same process; leaking the actor past the block
    would attribute the owner's own mid-conversation edits to the night's maintenance."""
    assert current_attribution().actor is Actor.ASSISTANT
    with acting_as(Actor.CURATOR, run_id="run7"):
        assert current_attribution().actor is Actor.CURATOR
    assert current_attribution().actor is Actor.ASSISTANT
    assert current_attribution().run_id is None
