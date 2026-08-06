"""The curator's lever on the judge: the rules it writes, and how they reach the rubric.

Two properties matter and nothing else enforces them. An edit must be additive — the curator writes
one rule a night and a wholesale overwrite would silently drop the ones already earning their keep.
And the base rubric, which is calibrated in code against `docs/judge_test_cases.md`, must survive
every edit: it is the half of the grading the nightly pass is deliberately not allowed to rewrite.
"""

from typing import cast

from app.assistant.judge import CuratedJudge
from app.assistant.judge_prompt import NISSE_JUDGE_PROMPT
from app.prompts import JudgeRulesTool, PromptStore, PromptType

CONVERSATION = 42


class _StoredPrompts:
    """The prompts collection as this test needs it: one text per type, read back as written."""

    def __init__(self, content: dict[PromptType, str] | None = None) -> None:
        self.content = content or {}

    async def get(self, prompt_type: PromptType) -> str | None:
        return self.content.get(prompt_type)

    async def set(self, prompt_type: PromptType, content: str) -> None:
        self.content[prompt_type] = content


def _tool(store: _StoredPrompts) -> JudgeRulesTool:
    return JudgeRulesTool(cast("PromptStore", store))


async def test_a_new_rule_is_added_beside_the_ones_already_there() -> None:
    store = _StoredPrompts({PromptType.JUDGE_RULES: "Верни ответ без цены."})

    result = await _tool(store).execute(add=["Верни рекомендацию без источников."])

    assert "updated" in result
    assert store.content[PromptType.JUDGE_RULES] == "Верни ответ без цены.\nВерни рекомендацию без источников."


async def test_a_rule_that_stopped_catching_anything_is_removed_by_a_fragment() -> None:
    store = _StoredPrompts({PromptType.JUDGE_RULES: "Верни ответ без цены.\nВерни ответ без источников."})

    await _tool(store).execute(remove=["без цены"])

    assert store.content[PromptType.JUDGE_RULES] == "Верни ответ без источников."


async def test_an_over_cap_edit_is_refused_and_writes_nothing() -> None:
    """Every line here makes redos more likely, so the cap is a real refusal — and the refusal must
    not half-apply the edit, or the block would drift with no record of what did land."""
    store = _StoredPrompts({PromptType.JUDGE_RULES: "Верни ответ без цены."})

    result = await _tool(store).execute(add=["x" * 2000])

    assert "Too long" in result
    assert store.content[PromptType.JUDGE_RULES] == "Верни ответ без цены."


async def test_re_sending_a_line_says_it_changed_nothing_rather_than_reporting_success() -> None:
    """Observed in a live pass: the tool injects the current block every turn, so the curator re-sent
    the rule it had just written, read `updated` back, took the "already present" note as proof the
    rule pre-existed, and told the owner it had changed nothing — while the store held its edit."""
    store = _StoredPrompts()
    tool = _tool(store)

    first = await tool.execute(add=["Верни ответ без цены."])
    second = await tool.execute(add=["Верни ответ без цены."])

    assert "updated" in first
    assert "NOT changed" in second
    assert "still stands" in second  # the report must not claim the earlier edit never happened
    assert store.content[PromptType.JUDGE_RULES] == "Верни ответ без цены."


async def test_the_curator_is_shown_the_rules_it_is_about_to_edit() -> None:
    """Editing from memory instead of from the record is how the block gains a rule it already had."""
    store = _StoredPrompts({PromptType.JUDGE_RULES: "Верни ответ без цены."})

    assert "Верни ответ без цены." in await _tool(store).system_prompt()


async def test_the_added_rules_reach_the_rubric_without_displacing_the_base_one() -> None:
    store = _StoredPrompts({PromptType.JUDGE_RULES: "Верни ответ без цены."})
    judge = CuratedJudge(cast("PromptStore", store), project="nisse2050")

    rubric = await judge.rubric()

    assert NISSE_JUDGE_PROMPT in rubric
    assert "Верни ответ без цены." in rubric


async def test_an_unedited_conversation_grades_on_the_base_rubric_alone() -> None:
    """No stored rules must mean no appended heading either — an empty 'ADDITIONAL RULES' section
    invites the judge to invent what belongs under it."""
    judge = CuratedJudge(cast("PromptStore", _StoredPrompts()), project="nisse2050")

    assert await judge.rubric() == NISSE_JUDGE_PROMPT
