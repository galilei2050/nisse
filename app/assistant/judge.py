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
from baski.agents.judge import JudgeUnavailableError  # not re-exported by the package
from google.auth.exceptions import GoogleAuthError
from pymongo.errors import PyMongoError

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

        Reading the rules and building that client both happen on the reply path now, where the loop
        treats only `JudgeUnavailableError` as "grade it anyway" (`baski.agents.Agent._grade`). A Mongo
        failover or an ADC token failure would otherwise escape `execute` and cost the owner a finished
        Opus answer — over optional extra lines the grade can run without.
        """
        try:
            instructions = await self.rubric()
            if self._judge is None or instructions != self._graded_by:
                self._judge = GeminiJudge(project=self._project, instructions=instructions)
                self._graded_by = instructions
                logger.info("Judge rubric loaded", extra={"chars": len(instructions)})
        except (PyMongoError, GoogleAuthError) as exc:
            raise JudgeUnavailableError(f"judge rubric unavailable: {exc}") from exc
        return self._judge

    async def rubric(self) -> str:
        """The instructions the next grade runs on: the base rubric, then the curator's added rules."""
        added = await self._store.get(PromptType.JUDGE_RULES)
        if not added:
            return NISSE_JUDGE_PROMPT
        # Each line is already a whole instruction ("Send back an answer that…" — the shape
        # `update_judge_rules` asks the curator for), so the heading names them and stops.
        return (
            f"{NISSE_JUDGE_PROMPT}\n\nADDITIONAL GROUNDS FOR A REDO, added from answers this owner rejected:\n{added}"
        )
