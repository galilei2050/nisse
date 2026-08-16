"""A Mongo stand-in for the two collections a review window is assembled from.

Shared by the evidence tests and the transcript-tool tests so both drive the real
`EvidenceCollector`: the tool's whole value is that it reaches a turn the window missed, and a
hand-written double of the collector would let that story pass without ever querying anything.

The fake honours `sort` and `length` on purpose. Every behaviour these tests assert — the last
reaction record wins, turns fold in order, the read walks BACKWARDS and stops at a budget — is
correct only because of the arguments the queries pass, so a fake that ignored them would stay green
against production that had lost them.
"""


class FakeCursor:
    def __init__(self, docs: list[dict]) -> None:
        self._docs = docs

    async def to_list(self, length: int | None = None) -> list[dict]:
        return self._docs if length is None else self._docs[:length]


class FakeCollection:
    def __init__(self, docs: list[dict]) -> None:
        self._docs = docs

    def find(self, flt: dict, sort: list[tuple[str, int]] | None = None) -> FakeCursor:
        found = [d for d in self._docs if self._match(d, flt)]
        for key, direction in reversed(sort or []):
            found.sort(key=lambda d, k=key: d[k], reverse=direction < 0)  # type: ignore[misc]  # bound per iteration
        return FakeCursor(found)

    @staticmethod
    def _match(doc: dict, flt: dict) -> bool:
        for key, condition in flt.items():
            if isinstance(condition, dict):  # the operators these queries use
                if "$gte" in condition and doc[key] < condition["$gte"]:
                    return False
                if "$lt" in condition and doc[key] >= condition["$lt"]:
                    return False
                if "$in" in condition and doc.get(key) not in condition["$in"]:
                    return False
            elif doc.get(key) != condition:
                return False
        return True


class FakeDatabase:
    def __init__(self, *, turns: list[dict], reactions: list[dict]) -> None:
        self._collections = {
            "conversation_turns": FakeCollection(turns),
            "reactions": FakeCollection(reactions),
        }

    def __getitem__(self, name: str) -> FakeCollection:
        return self._collections[name]
