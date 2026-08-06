"""The assistant's completeness judge, with the part of its rubric the curator maintains.

The rubric has two halves. `NISSE_JUDGE_PROMPT` is code: deploy-versioned, and changed only behind the
`replay-traces` regression harness, because a rule added on one axis is how the other silently
regresses. The added lines are data — one `judge_rules` document per conversation, written by the
nightly curator from answers the owner rejected, recorded in `revisions` like every other curator edit
and undoable the same way. Splitting it this way is what makes the rubric editable overnight without
putting the calibrated part one bad line away from being lost.

The lines are re-read on every grade, so an edit takes effect on the owner's next message rather than
at the next deploy — the conversation's agent is built once and cached, so anything read at build time
would sit stale until the process restarts.
"""

import logging

from baski.agents import GeminiJudge, Judge, Verdict

from app.assistant.judge_prompt import NISSE_JUDGE_PROMPT
from app.prompts import PromptStore, PromptType

logger = logging.getLogger(__name__)


class CuratedJudge(Judge):
    """Grades replies against the code rubric plus this conversation's added rules.

    Lifecycle: per-conversation — built with the chat's prompt store, reused for every reply.
    """

    def __init__(self, store: PromptStore, *, project: str) -> None:
        """Hold where the added rules are read from, and the GCP project the Gemini judge runs in."""
        self._store = store
        self._project = project
        self._graded_by = ""  # the rubric `_judge` was built with; a change here rebuilds it
        self._judge: GeminiJudge | None = None

    async def evaluate(self, transcript: str, answer: str, rules: str) -> Verdict:
        """Grade `answer` against the rubric as it stands right now."""
        judge = await self._current()
        return await judge.evaluate(transcript=transcript, answer=answer, rules=rules)

    async def _current(self) -> GeminiJudge:
        """The judge for today's rubric, rebuilt only when the stored lines actually changed.

        `GeminiJudge` takes its instructions at construction, and building one opens a Vertex client —
        so the rubric text is the cache key rather than rebuilding per grade.
        """
        instructions = await self.rubric()
        if self._judge is None or instructions != self._graded_by:
            logger.info("Judge rubric loaded", extra={"chars": len(instructions), "rebuilt": self._judge is not None})
            self._judge = GeminiJudge(project=self._project, instructions=instructions)
            self._graded_by = instructions
        return self._judge

    async def rubric(self) -> str:
        """The instructions the next grade runs on: the base rubric, then the curator's added rules."""
        added = await self._store.get(PromptType.JUDGE_RULES)
        if not added:
            return NISSE_JUDGE_PROMPT
        return f"{NISSE_JUDGE_PROMPT}\n\nADDITIONAL RULES — send an answer back when:\n{added}"
