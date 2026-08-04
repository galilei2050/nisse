"""The nightly curator — one agent pass that maintains what the assistant knows.

The assistant is a frozen model, so it cannot get smarter; what it CAN do is start tomorrow from a
better store. This pass reads a window of conversation plus the owner's reactions, classifies what
the owner was doing, and edits the four stores that shape the next reply. It runs off the request
path, once a night, so the owner never waits for it.

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
from typing import NamedTuple

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from baski.agents import Agent, AgentConfig, GeminiJudge, InMemoryMessageHistory, ToolSet
from baski.env import get_env
from baski.primitives import datetime
from baski.server.logger import log_context
from pymongo.asynchronous.database import AsyncDatabase

from app.curator.classify import Classification, MessageClassifier
from app.curator.evidence import Evidence, EvidenceCollector
from app.curator.prompt import CURATOR_JUDGE_PROMPT, NISSE_CURATOR_PROMPT, REVIEW_BRIEF
from app.curator.store import CuratorRun, CuratorRunStore
from app.shared import CoreDeps
from app.shared.revisions import Actor, RevisionLog, acting_as

logger = logging.getLogger(__name__)

_JUDGE_PROJECT = str(get_env("GOOGLE_CLOUD_PROJECT"))  # read at import — fail-fast if the secret is missing

CURATOR_MODEL = "claude-opus-5"  # runs once a night on high-stakes edits; a cheap miss here is expensive
CURATOR_TOOLS = ["memory", "lists", "core_memory", "subagents"]  # its whole surface — no web, no ask_user
_CONTEXT_TOKENS = 120_000  # a day of transcript plus the stores it reads back
_MAX_TURNS = 40
_WINDOW = datetime.timedelta(days=1)


# How the report is cut to Telegram's message limit. Taken as a dependency, not imported: the
# splitter lives in the chat layer, and a domain module reaching into the transport is the coupling
# `ScheduleRunner` was just freed from.
MessageSplitter = Callable[[str], list[str]]


class ReviewOutcome(NamedTuple):
    """What one review produced: the owner-facing report and what the pass cost."""

    report: str
    cost: float


# Reported when the review dies mid-pass. The tools commit their edits as they run, so a failure can
# leave the owner's stores already changed — and Cloud Scheduler does not retry, so an unreported
# half-pass would be a silent overnight rewrite with only orphaned revisions to show for it. How many
# edits actually landed is in the report header, which counts this run's revisions; the run id joins
# this message to the stack trace in the logs.
_CRASH_REPORT = "⚠️ Проход упал на середине. Сколько правок успело записаться — в шапке; трейс в логах (run {run_id})."


class Curator:
    """Runs one maintenance pass over one conversation. Lifecycle: long-lived, one per bot."""

    def __init__(self, deps: CoreDeps, *, bot: Bot, split_message: MessageSplitter) -> None:
        """Hold the shared clients, the bot the report goes out on, and the chat layer's splitter."""
        self._deps = deps
        self._bot = bot
        self._split_message = split_message
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
            # Stands until the review returns; settling in `finally` reports whichever survived.
            outcome = ReviewOutcome(report=_CRASH_REPORT.format(run_id=run_id), cost=0.0)
            try:
                outcome = await self._review(
                    conversation_id=conversation_id, run_id=run_id, evidence=evidence, classification=classification
                )
            finally:
                run = await self._settle(
                    run_id=run_id, evidence=evidence, classification=classification, outcome=outcome
                )
            logger.info("Curator pass finished", extra={"changes": run.changes, "cost": outcome.cost})
            return run

    async def _settle(
        self, *, run_id: str, evidence: Evidence, classification: Classification, outcome: ReviewOutcome
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
                report=outcome.report,
                cost=outcome.cost,
            )
        )
        await self._send_report(conversation_id=conversation_id, run=run)
        return run

    async def _review(
        self, *, conversation_id: int, run_id: str, evidence: Evidence, classification: Classification
    ) -> ReviewOutcome:
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
            result = await agent.execute()
        if result.response is None:
            raise RuntimeError(f"curator produced no report (trace {result.trace_id})")
        return ReviewOutcome(report=result.response, cost=result.total_cost)

    def _agent_config(self, conversation_id: int) -> AgentConfig:
        """The curator's own agent: its prompt, its four stores, a fresh history, its own judge.

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
            model=CURATOR_MODEL,
            message_history=InMemoryMessageHistory(max_tokens=_CONTEXT_TOKENS),
            anthropic_client=self._deps.anthropic,
            database=self._deps.database,
            bucket_name=self._deps.bucket_name,
            system_prompt=NISSE_CURATOR_PROMPT,
            judge=GeminiJudge(instructions=CURATOR_JUDGE_PROMPT, project=_JUDGE_PROJECT),
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

    async def _send_report(self, *, conversation_id: int, run: CuratorRun) -> None:
        """Message the owner what the pass did, split to Telegram's size limit like every other send.

        A night with many changes runs past 4096 characters, and an over-long message is rejected
        whole — the report would vanish while the edits stood. Degrades on a transport failure only:
        the edits are durable and in the run record, so a Telegram outage must not undo a good pass.
        """
        header = f"🌙 Ночная уборка · изменений: {run.changes} · разобрано сообщений: {run.owner_messages}"
        try:
            for chunk in self._split_message(f"{header}\n\n{run.report}"):
                await self._bot.send_message(chat_id=conversation_id, text=chunk)
        except TelegramAPIError:
            logger.warning("Curator report not delivered; changes stand and are in the run record", exc_info=True)

    @staticmethod
    async def ensure_indexes(database: AsyncDatabase) -> None:
        """Index for the curator's own collection. Idempotent; called at startup.

        `revisions` is NOT here: every actor writes it, the assistant's tools included, so its index
        would vanish with the curator wiring while the writes went on.
        """
        await CuratorRunStore.ensure_indexes(database)
