"""ANON003: a free function must not take a long-lived collaborator as a parameter.

The rule exists because the convention alone does not hold — a prompt rule is read or not read, and
the smell it catches is the one that quietly grows a second copy of behaviour somewhere else. So the
rule itself needs to fail when it stops working: a check that silently matches nothing is worse than
no check, because `make lint` stays green either way.
"""

from pathlib import Path

from anon_lint import lint_source

PATH = Path("example.py")


def _codes(source: str) -> list[str]:
    return [f.code for f in lint_source(source, PATH)]


def test_a_free_function_taking_a_client_is_flagged() -> None:
    """The shape this whole convention is about: the client is threaded in at every call site."""
    source = "async def classify(anthropic: AsyncAnthropic, evidence: Evidence) -> Classification: ...\n"

    (finding,) = lint_source(source, PATH)

    assert finding.code == "ANON003"
    assert "classify(anthropic: AsyncAnthropic)" in finding.message


def test_a_database_a_bot_and_a_store_all_count() -> None:
    """One parameter type per line would rot; the rule matches a named set plus `*Store`-style names."""
    assert _codes("def a(database: AsyncDatabase) -> None: ...") == ["ANON003"]
    assert _codes("def b(bot: Bot) -> None: ...") == ["ANON003"]
    assert _codes("def c(store: MemoryStore) -> None: ...") == ["ANON003"]
    assert _codes("def d(log: RevisionLog) -> None: ...") == ["ANON003"]


def test_a_method_is_not_flagged_because_it_is_already_bound() -> None:
    """The fix for ANON003 is "make it a method" — flagging methods would make the fix unreachable."""
    source = "class Curator:\n    def take(self, database: AsyncDatabase) -> None: ...\n"

    assert _codes(source) == []


def test_a_tool_factory_taking_CoreDeps_is_not_flagged() -> None:
    """`(deps, conversation_id) -> list[Tool]` is the tool registry's contract, and passing CoreDeps
    around is what CoreDeps is FOR — flagging it would push the codebase to fight its own wiring."""
    source = "def memory_tools(deps: CoreDeps, conversation_id: int) -> list[Tool]: ...\n"

    assert _codes(source) == []


def test_a_dependency_inside_a_generic_or_a_string_annotation_still_counts() -> None:
    """`"X"` under TYPE_CHECKING and `X[Any]` are the two ways the same parameter is spelled here."""
    assert _codes('def a(store: "MemoryStore") -> None: ...') == ["ANON003"]
    assert _codes("def b(collection: AsyncCollection[Any]) -> None: ...") == ["ANON003"]


def test_a_pure_helper_over_primitives_is_left_alone() -> None:
    """The rule must not push stateless text helpers into classes — that is the ceremony it would
    otherwise trade the duplication for."""
    source = "def split_message(text: str, limit: int = 4096) -> list[str]: ...\n"

    assert _codes(source) == []


def test_noqa_suppresses_it_on_the_def_line() -> None:
    """A documented exception (a thin wrapper over one library call) has to be expressible, or the
    rule gets deleted the first time it is inconvenient."""
    source = "def ensure_index(collection: AsyncCollection) -> None:  # noqa: ANON003 — wraps one pymongo call\n    ...\n"

    assert _codes(source) == []


def test_one_finding_per_function_even_with_several_dependencies() -> None:
    """The fix is the same whichever parameter tripped it; three lines of noise would train the eye
    to skip the rule."""
    source = "def fire(database: AsyncDatabase, bot: Bot, store: MemoryStore) -> None: ...\n"

    assert _codes(source) == ["ANON003"]
