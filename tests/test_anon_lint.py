"""ANON003: a free function must not take a long-lived collaborator as a parameter.

The rule exists because the convention alone does not hold — a prompt rule is read or not read, and
the smell it catches is the one that quietly grows a second copy of behaviour somewhere else. So the
rule itself needs to fail when it stops working: a check that silently matches nothing is worse than
no check, because `make lint` stays green either way.
"""

from pathlib import Path

import pytest

from anon_lint import lint_source, main

PATH = Path("example.py")


def _codes(source: str) -> list[str]:
    return [f.code for f in lint_source(source, PATH)]


def test_the_cli_exits_nonzero_and_names_the_spot(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The exit code IS the enforcement — `make lint` reads nothing else.

    Every other test here calls `lint_source` directly and would stay green with `main` returning 0
    unconditionally, which is the "green forever, catches nothing" failure this file exists to stop.
    The printed `path:line:col` is pinned too: a linter that reports every violation at line 1 is one
    nobody acts on.
    """
    violator = tmp_path / "violator.py"
    violator.write_text("def mark_done(database: AsyncDatabase) -> None: ...\n", encoding="utf-8")

    exit_code = main([str(violator)])

    assert exit_code == 1
    assert f"{violator}:1:0: ANON003" in capsys.readouterr().out


def test_the_cli_exits_zero_on_clean_input(tmp_path: Path) -> None:
    """The other half: a rule that fails on everything gets switched off within a day."""
    clean = tmp_path / "clean.py"
    clean.write_text("def split_message(text: str) -> list[str]: ...\n", encoding="utf-8")

    assert main([str(clean)]) == 0


def test_a_free_function_taking_a_client_is_flagged() -> None:
    """The shape this whole convention is about: the client is threaded in at every call site.

    The position is asserted, not just the code: a finding anchored anywhere but the `def` line makes
    both the report and the `# noqa` escape hatch point at the wrong place.
    """
    source = "async def classify(anthropic: AsyncAnthropic, evidence: Evidence) -> Classification: ...\n"

    (finding,) = lint_source(source, PATH)

    assert (finding.code, finding.line, finding.col) == ("ANON003", 1, 0)
    assert "classify(anthropic: AsyncAnthropic)" in finding.message


def test_a_database_a_bot_and_a_store_all_count() -> None:
    """One parameter type per line would rot; the rule matches a named set plus `*Store`-style names."""
    assert _codes("def a(database: AsyncDatabase) -> None: ...") == ["ANON003"]
    assert _codes("def b(bot: Bot) -> None: ...") == ["ANON003"]
    assert _codes("def c(store: MemoryStore) -> None: ...") == ["ANON003"]
    assert _codes("def d(log: RevisionLog) -> None: ...") == ["ANON003"]


def test_a_keyword_only_dependency_counts() -> None:
    """Keyword-only is how this repo writes almost every collaborator (`def f(*, database: ...)`), so
    a rule that only walked positional parameters would miss the shape it was written for."""
    assert _codes("def a(*, database: AsyncDatabase) -> None: ...") == ["ANON003"]
    assert _codes("def b(bot: Bot, /) -> None: ...") == ["ANON003"]


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


def test_making_the_dependency_optional_does_not_silence_it() -> None:
    """Otherwise ` | None` is a one-token way to shut the rule up without changing the design — and
    a dependency that is optional is a worse smell, not an exemption."""
    assert _codes("def a(store: MemoryStore | None) -> None: ...") == ["ANON003"]
    assert _codes("def b(store: Optional[MemoryStore]) -> None: ...") == ["ANON003"]
    assert _codes('def c(store: "MemoryStore | None") -> None: ...') == ["ANON003"]


def test_this_apps_own_collaborators_count_not_just_third_party_clients() -> None:
    """The rule file's own BAD example is `resolve_tap(questions: PendingQuestions, ...)`. A list of
    only third-party client types would miss every in-house collaborator, which is most of them."""
    assert _codes("def a(questions: PendingQuestions) -> bool: ...") == ["ANON003"]
    assert _codes("def b(transcriber: Transcriber) -> str: ...") == ["ANON003"]
    assert _codes("def c(scheduling: SchedulingService) -> None: ...") == ["ANON003"]


def test_a_pure_helper_over_primitives_is_left_alone() -> None:
    """The rule must not push stateless text helpers into classes — that is the ceremony it would
    otherwise trade the duplication for."""
    source = "def split_message(text: str, limit: int = 4096) -> list[str]: ...\n"

    assert _codes(source) == []


def test_noqa_suppresses_it_on_the_def_line_of_a_wrapped_signature() -> None:
    """A documented exception has to be expressible, or the rule gets deleted the first time it is
    inconvenient — and this repo wraps long signatures, so the `def` line and the offending parameter
    are different lines. Anchoring the finding on the parameter instead would put the escape hatch
    somewhere the author cannot see it.
    """
    source = "def fire(  # noqa: ANON003 — wraps one library call\n    database: AsyncDatabase,\n) -> None: ...\n"

    assert _codes(source) == []


def test_the_noqa_reason_may_be_written_any_way_and_still_suppresses() -> None:
    """`CLAUDE.md` requires the noqa to name a reason, so the reason text must not be parsed as part
    of the code — otherwise the mandated form is the one that silently fails to suppress, and the
    author sees an error they cannot turn off."""
    for comment in ("# noqa: ANON003 — wraps one call", "# noqa: ANON003 wraps one call", "# noqa: ANON003"):
        assert _codes(f"def f(db: AsyncDatabase) -> None:  {comment}\n    ...\n") == [], comment
    assert _codes("def f(db: AsyncDatabase, x: dict[str, Any]) -> None:  # noqa: ANON003, ANON002 — both\n    ...") == []


def test_the_older_rules_still_match_something() -> None:
    """ANON001/ANON002 predate this file and were never covered.

    Emptying their name sets leaves `make lint` green and turns all 37 `# noqa: ANON00{1,2}` in
    `app/` into decoration, with no second signal — `external = [...]` in pyproject is exactly what
    stops ruff from reporting them as unused. Two assertions are enough to notice.
    """
    assert _codes("def f(x: dict[str, Any]) -> None: ...") == ["ANON002"]
    assert _codes("def f(x: tuple[int, str]) -> None: ...") == ["ANON001"]


def test_one_finding_per_function_even_with_several_dependencies() -> None:
    """The fix is the same whichever parameter tripped it; three lines of noise would train the eye
    to skip the rule."""
    source = "def fire(database: AsyncDatabase, bot: Bot, store: MemoryStore) -> None: ...\n"

    assert _codes(source) == ["ANON003"]
