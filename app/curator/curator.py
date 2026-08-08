"""The nightly curator — one agent pass that maintains what the assistant knows.

The assistant is a frozen model, so it cannot get smarter; what it CAN do is start tomorrow from a
better store. This pass reads a window of conversation plus the owner's reactions, classifies what
the owner was doing, and edits the five stores that shape the next reply. It normally runs off the
request path, once a night, so the owner never waits for it; `/curate` runs the same pass on demand,
and there they do wait.

Two properties make it safe enough to run unattended, and both are load-bearing:

- **Every write is attributed and reversible.** The run wraps its work in `acting_as(CURATOR)`, so
  each store records the text it replaced against this run id (`app/shared/revisions.py`). A change
  the owner disagrees with is readable and undoable, rather than an unexplained difference in
  behaviour they discover weeks later.
- **The owner gets told.** The pass ends by messaging its report. Silent self-modification is what
  makes an assistant untrustworthy — the owner cannot audit what they never saw happen.

The curator uses the SAME tools as the live assistant (built from the shared registry), so there is
one write path per store, not a parallel curator-only one that could drift from it.
"""

import logging
import secrets
from collections.abc import Callable

from aiogram.exceptions import TelegramAPIError
from baski.agents import Agent, AgentConfig, AgentExecuteResult, GeminiJudge, InMemoryMessageHistory, ToolSet
from baski.primitives import datetime
from baski.server.logger import log_context
from pymongo.asynchronous.database import AsyncDatabase

from app.curator.classify import Classification, MessageClassifier
from app.curator.evidence import Evidence, EvidenceCollector
from app.curator.prompt import CURATOR_JUDGE_PROMPT, NISSE_CURATOR_PROMPT, REVIEW_BRIEF
from app.curator.store import CuratorRun, CuratorRunStore
from app.shared import CoreDeps, MessageSender
from app.shared.revisions import Actor, RevisionLog, acting_as

logger = logging.getLogger(__name__)


CURATOR_MODEL = "claude-opus-5"  # runs once a night on high-stakes edits; a cheap miss here is expensive
# Its whole surface — no web, no ask_user. `judge_rules` and `subagents` are here and deliberately not
# in MAIN_TOOLS: both decide how the assistant behaves for every later reply, so only the attributed,
# reported nightly pass writes them.
CURATOR_TOOLS = ["memory", "lists", "core_memory", "judge_rules", "subagents"]
_CONTEXT_TOKENS = 120_000  # a day of transcript plus the stores it reads back
_MAX_TURNS = 40
_WINDOW = datetime.timedelta(days=1)


# How a finished review becomes the message the owner reads. Taken as a dependency, not imported:
# `app.chat` drives this pass from `/curate`, so importing the chat layer back would cycle.
ReportFormatter = Callable[[AgentExecuteResult], str]


# Reported when the review dies mid-pass. The tools commit their edits as they run, so a failure can
# leave the owner's stores already changed — and Cloud Scheduler does not retry, so an unreported
# half-pass would be a silent overnight rewrite with only orphaned revisions to show for it. How many
# edits actually landed is in the report header, which counts this run's revisions; the run id joins
# this message to the stack trace in the logs.
_CRASH_REPORT = "⚠️ Проход упал на середине. Сколько правок успело записаться — в шапке; трейс в логах (run {run_id})."


class Curator:
    """Runs one maintenance pass over one conversation. Lifecycle: long-lived, one per bot."""

    def __init__(self, deps: CoreDeps, *, sender: MessageSender, format_report: ReportFormatter) -> None:
        """Hold the shared clients, the channel the report goes out on, and how the report is rendered."""
        self._deps = deps
        self._sender = sender
        self._format_report = format_report
        self._runs = CuratorRunStore(deps.database)
        self._evidence = EvidenceCollector(deps.database)
        self._classifier = MessageClassifier(deps.anthropic)

    async def active_conversations(self) -> list[int]:
        """Conversations with a turn in the review window — the ones with anything to learn from."""
        return await self._evidence.active_conversations(since=datetime.now() - _WINDOW)

    async def curate(self, *, conversation_id: int, window: datetime.timedelta = _WINDOW) -> CuratorRun:
        """Review the window and maintain the stores; returns the recorded run.

        A window with nothing in it still records a run — "the curator ran and found nothing" and
        "the curator never ran" must not look the same to whoever reads the history later.
        """
        run_id = secrets.token_hex(6)  # stamps every revision this pass writes
        since = datetime.now() - window
        with log_context(conversationId=conversation_id, agent="curator", runId=run_id):
            evidence = await self._evidence.collect(conversation_id=conversation_id, since=since)
            if not evidence.exchanges:
                return await self._record_idle(run_id, conversation_id=conversation_id, since=since)

            classification = await self._classifier.classify(evidence)
            # Assigned before the raise it may survive, so `finally` settles on whatever the review
            # reached: an agent that returned and then failed its own check still cost the owner money.
            review: AgentExecuteResult | None = None
            try:
                review = await self._review(
                    conversation_id=conversation_id, run_id=run_id, evidence=evidence, classification=classification
                )
                if review.response is None:
                    raise RuntimeError(f"curator produced no report (trace {review.trace_id})")
            finally:
                run = await self._settle(run_id=run_id, evidence=evidence, classification=classification, review=review)
            logger.info("Curator pass finished", extra={"changes": run.changes, "cost": run.cost})
            return run

    async def _settle(
        self, *, run_id: str, evidence: Evidence, classification: Classification, review: AgentExecuteResult | None
    ) -> CuratorRun:
        """Count what the pass changed, record the run, and tell the owner. Runs on both exits."""
        conversation_id = evidence.conversation_id
        changes = await RevisionLog(self._deps.database, conversation_id=conversation_id).for_run(run_id)
        run = await self._runs.record(
            CuratorRun(
                conversation_id=conversation_id,
                run_id=run_id,
                since=evidence.since,
                exchanges_reviewed=len(evidence.exchanges),
                owner_messages=evidence.owner_message_count,
                reactions_reviewed=evidence.reaction_count,
                signals=[f"{s.kind}: {s.about}" for s in classification.signals],
                changes=len(changes),
                report=review.response if review and review.response else _CRASH_REPORT.format(run_id=run_id),
                cost=review.total_cost if review else 0.0,
            )
        )
        await self._send_report(conversation_id=conversation_id, run=run, review=review)
        return run

    async def _review(
        self, *, conversation_id: int, run_id: str, evidence: Evidence, classification: Classification
    ) -> AgentExecuteResult:
        """Run the agent over the brief, with every store write attributed to this run.

        `acting_as` spans the whole loop rather than each tool call: the tools are the assistant's
        own, and wrapping the run is what lets them stay unaware of who is driving them.
        """
        agent = Agent(self._agent_config(conversation_id))
        agent.add_pinned_text(
            REVIEW_BRIEF.format(
                digest=evidence.render(),
                classification=classification.render(),
            )
        )
        with acting_as(Actor.CURATOR, run_id=run_id):
            return await agent.execute()

    def _agent_config(self, conversation_id: int) -> AgentConfig:
        """The curator's own agent: its prompt, its five stores, a fresh history, its own judge.

        NOT the assistant's judge — that rubric grades how completely an answer served the owner's
        request, and would push a maintenance pass toward doing more work on thin evidence. The
        curator's rubric (`CURATOR_JUDGE_PROMPT`) grades the opposite discipline: is every claimed
        change backed by something the owner said, and is a quiet night reported as one.
        """
        toolset = ToolSet()
        for tool in self._deps.tools.build(CURATOR_TOOLS, self._deps, conversation_id):
            toolset.add(tool)
        return AgentConfig(
            toolset=toolset,
            name="curator",  # how this run is labelled in `traces`
            model=CURATOR_MODEL,
            message_history=InMemoryMessageHistory(max_tokens=_CONTEXT_TOKENS),
            anthropic_client=self._deps.anthropic,
            database=self._deps.database,
            bucket_name=self._deps.bucket_name,
            system_prompt=NISSE_CURATOR_PROMPT,
            judge=GeminiJudge(instructions=CURATOR_JUDGE_PROMPT, project=self._deps.judge_project),
            max_turns=_MAX_TURNS,
            await_trace=self._deps.await_trace,
            local_traces_dir=self._deps.local_traces_dir,
        )

    async def _record_idle(self, run_id: str, *, conversation_id: int, since: datetime.datetime) -> CuratorRun:
        """Record a pass that had nothing to review — no model call, no message to the owner."""
        logger.info("Curator found no conversation in the window")
        return await self._runs.record(
            CuratorRun(
                conversation_id=conversation_id,
                run_id=run_id,
                since=since,
                exchanges_reviewed=0,
                owner_messages=0,
                reactions_reviewed=0,
                signals=[],
                changes=0,
                report="",
                cost=0.0,
            )
        )

    async def _send_report(self, *, conversation_id: int, run: CuratorRun, review: AgentExecuteResult | None) -> None:
        """Message the owner what the pass did — rendered as a reply is: verdict and cost included.

        A pass that died has no result to render, so it sends the bare crash notice. The header says
        "разбор дня", not "ночная уборка": `/curate` runs the same pass at any hour.

        Degrades on a transport failure only: the edits are durable and in the run record, so a
        Telegram outage must not undo a good pass.
        """
        header = f"🌙 Разбор дня · изменений: {run.changes} · разобрано сообщений: {run.owner_messages}"
        body = self._format_report(review) if review and review.response else run.report
        try:
            await self._sender.send(chat_id=conversation_id, text=f"{header}\n\n{body}")
        except TelegramAPIError:
            logger.warning("Curator report not delivered; changes stand and are in the run record", exc_info=True)

    @staticmethod
    async def ensure_indexes(database: AsyncDatabase) -> None:
        """Index for the curator's own collection. Idempotent; called at startup.

        `revisions` is NOT here: every actor writes it, the assistant's tools included, so its index
        would vanish with the curator wiring while the writes went on.
        """
        await CuratorRunStore.ensure_indexes(database)
