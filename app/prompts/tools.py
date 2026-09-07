"""The two prompt blocks the bot maintains about itself: core memory, and the judge's added rules.

Both are edited the same way — a block of lines, added and removed one at a time (`list_edit`-style)
rather than overwritten wholesale, which silently dropped rules — and both are read live rather than
frozen at build time. That shared shape is `_PromptLinesTool`; each concrete tool declares which
prompt it edits, how big it may get, and how it reads to the model that receives it.

- CORE MEMORY holds, in three loose sections, the standing context that must shape (almost) every
  reply: BEHAVIOUR rules, canonical owner IDENTITY, the owner's CURRENT FOCUS. It is injected into
  the system prompt every turn through `system_prompt()`. Everything topical lives in `memories`
  (recalled on demand), never here.
- JUDGE RULES are appended to the completeness rubric a second model grades every reply against
  (`app/assistant/judge.py`). The base rubric stays in code — it is deploy-versioned and regression-
  tested through the replay harness — so what the curator maintains is the added lines, and a bad one
  is a line to drop rather than a rewritten rubric to reconstruct.

Both blocks are size-capped: core memory costs tokens on every turn, judge rules on every grade.
"""

from typing import ClassVar, NamedTuple

from baski.agents.tool import Tool
from pydantic import BaseModel, Field

from app.prompts.store import PromptStore, PromptType
from app.shared import CoreDeps
from app.shared.text import match_unique
from app.tools.registry import ToolRegistrar

# Always-on = pure per-turn token cost, so it's hard-capped; the agent must keep only what earns a slot.
# ~4500 chars ≈ 1125 tokens/turn. Raised x1.5 from 3000 — real traffic kept the block pinned at the cap,
# so standing rules were still getting dropped for lack of room.
_CORE_BUDGET = 4500  # characters

_CORE_HEADER = (
    "CORE MEMORY — your always-on standing context (the only owner-knowledge injected automatically). "
    "Follow the BEHAVIOUR rules; treat CURRENT FOCUS as live context."
)
_CORE_EMPTY = (
    "(empty — when the owner states a standing behaviour rule, an identity fact that shapes most "
    "turns, or a current focus/goal, add it here with update_core_memory.)"
)

# The rubric is read on every grade, and a sprawling one grades worse — the judge is asked to be
# conservative, and a long tail of added clauses is exactly what makes it demand redos. A third of
# core memory's budget is room for the handful of rules the owner actually keeps hitting.
_JUDGE_BUDGET = 1500  # characters

# The registry name the judge-rubric editor is built under. A constant because the sub-agent write
# guard refuses it by name (`app/subagents/tools.py`): "curator-only" is not enforced by absence from
# MAIN_TOOLS alone — the main agent delegates to sub-agents, whose tool_names resolve any registered
# name — so the two places that must agree on the spelling share it.
JUDGE_RULES_TOOL_NAME = "judge_rules"

_JUDGE_HEADER = (
    "JUDGE RULES — the lines the nightly curator added to the assistant's completeness rubric, from "
    "answers the owner rejected. This is what the judge grades replies against, on top of the base "
    "rubric in code. Shown live, so it ALREADY INCLUDES anything you added in this pass: a rule here "
    "is not evidence it predates you."
)
_JUDGE_EMPTY = "(empty — nothing has been added to the rubric yet.)"


class _RemoveOutcome(NamedTuple):
    """Result of removing lines: the kept lines, and notes for terms that were ambiguous or missing."""

    kept: list[str]
    notes: list[str]


def _remove_lines(lines: list[str], terms: list[str], *, label: str) -> _RemoveOutcome:
    """Drop lines matched by each term (exact line or unique fragment); report ambiguous/missing terms.

    `label` names the block back to the model: this runs for every prompt the bot maintains, and a
    miss reported against the wrong store is a curator told its judge rule is absent from core memory.
    """
    candidates = [ln for ln in lines if ln.strip()]
    drop: set[str] = set()
    notes: list[str] = []
    for term in terms:
        hits = match_unique(candidates, term)
        if len(hits) == 1:
            drop.add(hits[0])
        elif len(hits) > 1:
            notes.append(f"ambiguous (matches several — be more specific): {term}")
        else:
            notes.append(f"not in {label}: {term}")
    return _RemoveOutcome([ln for ln in lines if ln not in drop], notes)


class _AddOutcome(NamedTuple):
    """Result of adding lines: the resulting block, and which items were already in it."""

    lines: list[str]
    skipped: list[str]


def _add_lines(lines: list[str], items: list[str]) -> _AddOutcome:
    """Append items not already present (case-insensitive), preserving order; report the ones skipped."""
    present = {ln.strip().lower() for ln in lines if ln.strip()}
    skipped: list[str] = []
    for item in items:
        if not item.strip():
            continue
        if item.strip().lower() in present:
            skipped.append(item)
            continue
        lines.append(item)
        present.add(item.strip().lower())
    return _AddOutcome(lines, skipped)


class _PromptLinesTool(Tool):
    """Edits one stored prompt block line by line, and renders it back. Lifecycle: per-conversation.

    A subclass says WHICH prompt (`prompt_type`), how big it may grow (`budget`), and how the block
    reads to whoever receives it (`header` / `empty`). The editing itself — remove named lines, append
    new ones, refuse an over-cap result — is identical for every prompt the bot maintains about
    itself, and lives here so the two can't drift into different edit semantics.
    """

    label: ClassVar[str]  # what this block is called back to the model that edited it
    prompt_type: ClassVar[PromptType]
    budget: ClassVar[int]
    header: ClassVar[str]
    empty: ClassVar[str]
    over_budget_hint: ClassVar[str]

    class Input(BaseModel):
        """Arguments for one edit — add and/or remove whole lines (mirrors list_edit)."""

        add: list[str] = Field(
            default_factory=list,
            description="Lines to add (one concise rule/fact each); duplicates already present are skipped",
        )
        remove: list[str] = Field(
            default_factory=list,
            description="Lines to remove: the exact line, or a short distinctive fragment of a longer one "
            "(removed only when it uniquely matches one line)",
        )

    def __init__(self, store: PromptStore) -> None:
        """Hold the prompt store this block is read from and written to."""
        self._store = store

    async def execute(self, *, add: list[str] | None = None, remove: list[str] | None = None) -> str:
        """Apply remove then add over the block's lines; reject if the result exceeds the cap. Mirrors list_edit.

        The result names what actually landed, because the caller cannot see the block change: this
        tool injects the CURRENT block every turn, so a second call re-sending a line it wrote a
        moment ago reads its own edit back as something that was already there — and, told only
        "updated", reports to the owner that nothing changed while the store says otherwise.
        """
        if not add and not remove:
            return "Nothing to do — pass lines to add and/or remove."
        stored = (await self._store.get(self.prompt_type) or "").strip("\n")
        lines = stored.split("\n")
        notes: list[str] = []
        if remove:
            lines, notes = _remove_lines(lines, remove, label=self.label.lower())
        skipped: list[str] = []
        if add:
            lines, skipped = _add_lines(lines, add)
        if skipped:
            notes.append(f"already there, not added again: {'; '.join(skipped)}")
        updated = "\n".join(lines).strip("\n")
        if len(updated) > self.budget:
            return f"Too long: {len(updated)} chars > {self.budget} cap. {self.over_budget_hint}"
        if updated == stored:
            return (
                f"{self.label} NOT changed — the block already reads exactly this. Anything you wrote "
                f"earlier in this pass still stands; do not report it as missing."
                + (f"\n({'; '.join(notes)})" if notes else "")
            )
        await self._store.set(self.prompt_type, updated)
        kept = len([line for line in lines if line.strip()])
        return f"{self.label} updated — {kept} line(s) now." + (f"\n({'; '.join(notes)})" if notes else "")

    async def system_prompt(self) -> str:
        """The block as it stands, read live and injected into its agent's system prompt every turn.

        For core memory that injection IS the feature — it is how the always-on block reaches the
        reply. For judge rules it is what stops the curator editing a block it is remembering rather
        than reading; the judge itself gets these lines from the store, not from here.
        """
        content = await self._store.get(self.prompt_type)
        return f"{self.header}\n\n{content or self.empty}"


class CoreMemoryTool(_PromptLinesTool):
    """Maintain the always-on core-memory block injected into the system every turn. Lifecycle: per-conversation."""

    name = "update_core_memory"
    one_line = "Edit your always-on core memory like a list — add and/or remove whole lines in one call"
    description = (
        "Edit your CORE MEMORY (the always-on block) like a list: add and/or remove whole lines in one "
        "call. Touch ONLY the lines you name — never rewrite the block wholesale (that silently drops "
        "rules). To replace a rule, remove the old line and add the new one in the same call. Remove by "
        "the exact line or a distinctive fragment of it (dropped only if it uniquely matches; else you're "
        "asked to be more specific). You see the current block every turn — don't add a line already "
        "covered. Sections: BEHAVIOUR (how to act/speak/address the owner); ABOUT THE OWNER (canonical "
        "identity that silently shapes most turns — timezone/language/name; store the operational value, "
        "e.g. timezone not 'lives in X'); CURRENT FOCUS (active goals). "
        "ROUTING: add something ONLY if ignoring it would make you wrong on a turn that never mentions it; "
        "if it matters only once its topic comes up, use recall_save (LONG-TERM MEMORY) instead. A "
        "long-term memory is NOT injected and does NOT substitute for CORE MEMORY — never skip writing here "
        "because a similar memory exists, and don't search memory first. CORE is owner-knowledge only — how "
        "YOU operate (context pruning, tool workflows) is NOT core memory. Keep each line tight — costs tokens "
        "every turn."
    )

    label = "Core memory"
    prompt_type = PromptType.CORE_MEMORY
    budget = _CORE_BUDGET
    header = _CORE_HEADER
    empty = _CORE_EMPTY
    # NOT "drop a weaker line to fit": the block sits at its cap permanently, so that advice turns
    # every add into the eviction of a standing rule nobody chose to lose. The refusal is the answer;
    # the finding belongs in a memory.
    over_budget_hint = "Put this in recall_save instead — do NOT delete a standing line to make room."


class JudgeRulesTool(_PromptLinesTool):
    """Maintain the lines added to the completeness rubric the judge grades replies by.

    Lifecycle: per-conversation. Curator-only, like `subagent_save`: these lines decide when a reply
    is sent back to be redone, so nothing the owner types in chat reaches this write path.
    """

    name = "update_judge_rules"
    label = "Judge rules"
    prompt_type = PromptType.JUDGE_RULES
    budget = _JUDGE_BUDGET
    header = _JUDGE_HEADER
    empty = _JUDGE_EMPTY
    over_budget_hint = "Remove the rule that has stopped catching anything, in the same call."
    one_line = "Edit the rules added to the assistant's completeness rubric — add and/or remove whole lines"
    description = (
        "Edit the JUDGE RULES like a list: add and/or remove whole lines in one call. A second model "
        "grades every reply the assistant writes against a base rubric (in code) PLUS these lines, and "
        "sends the reply back to be redone when it falls short. Add a line when the owner rejected an "
        "answer for a reason the rubric would not have caught — a missing deliverable the ask implied, a "
        "shape of answer they keep having to ask for twice. Write it as a condition for sending an "
        "answer BACK, e.g. 'Send back an answer that recommends an option without naming what it costs.' "
        "Do NOT add taste, tone or length rules: every line makes redos more likely, and a redo costs "
        "the owner a full regeneration they see as a near-duplicate. This is not the place for facts "
        "about the owner (recall_save) or standing behaviour (update_core_memory) — only for what makes "
        "an answer incomplete."
    )


def core_memory_tools(deps: CoreDeps, conversation_id: int) -> list[Tool]:
    """The always-on core-memory block editor, over a chat-scoped prompt store."""
    return [CoreMemoryTool(PromptStore(deps.database, conversation_id=conversation_id))]


def judge_rules_tools(deps: CoreDeps, conversation_id: int) -> list[Tool]:
    """The judge-rubric editor, over a chat-scoped prompt store (curator-only roster)."""
    return [JudgeRulesTool(PromptStore(deps.database, conversation_id=conversation_id))]


def register_tools(registrar: ToolRegistrar) -> None:
    """Register both prompt editors under the names their agents' tool specs reference."""
    registrar.register("core_memory", core_memory_tools)
    registrar.register(JUDGE_RULES_TOOL_NAME, judge_rules_tools)
