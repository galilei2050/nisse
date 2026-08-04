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
from typing import NamedTuple

from aiogram import Bot
from baski.agents import Agent, AgentConfig, GeminiJudge, InMemoryMessageHistory, ToolSet
from baski.env import get_env
from baski.primitives import datetime
from baski.server.logger import log_context
from pymongo.asynchronous.database import AsyncDatabase

from app.curator.classify import Classification, classify
from app.curator.evidence import Evidence, collect, render
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
_TURNS = "conversation_turns"


class ReviewOutcome(NamedTuple):
    """What one review produced: the owner-facing report and what the pass cost."""

    report: str
    cost: float


def _run_id() -> str:
    """A short id for one pass — what every revision it writes is stamped with."""
    return secrets.token_hex(6)


class Curator:
    """Runs one maintenance pass over one conversation. Lifecycle: long-lived, one per bot."""

    def __init__(self, deps: CoreDeps, *, bot: Bot) -> None:
        """Hold the shared clients; the bot is how the report reaches the owner."""
        self._deps = deps
        self._bot = bot
        self._runs = CuratorRunStore(deps.database)

    async def active_conversations(self, *, window: datetime.timedelta = _WINDOW) -> list[int]:
        """Conversations with a turn in the window — the ones with anything to learn from."""
        since = datetime.now() - window
        ids = await self._deps.database[_TURNS].distinct("conversation_id", {"created_at": {"$gte": since}})
        logger.info("Curator sweep", extra={"conversations": len(ids)})
        return sorted(ids)

    async def curate(self, *, conversation_id: int, window: datetime.timedelta = _WINDOW) -> CuratorRun:
        """Review the window and maintain the stores; returns the recorded run.

        A window with nothing in it still records a run — "the curator ran and found nothing" and
        "the curator never ran" must not look the same to whoever reads the history later.
        """
        run_id = _run_id()
        since = datetime.now() - window
        with log_context(conversationId=conversation_id, agent="curator", runId=run_id):
            evidence = await collect(self._deps.database, conversation_id=conversation_id, since=since)
            if not evidence.exchanges:
                return await self._record_idle(run_id, conversation_id=conversation_id, since=since)

            classification = await classify(self._deps.anthropic, evidence)
            outcome = await self._review(
                conversation_id=conversation_id, run_id=run_id, evidence=evidence, classification=classification
            )
            changes = await RevisionLog(self._deps.database, conversation_id=conversation_id).for_run(run_id)
            run = await self._runs.record(
                CuratorRun(
                    conversation_id=conversation_id,
                    run_id=run_id,
                    since=since,
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
            logger.info("Curator pass finished", extra={"changes": len(changes), "cost": outcome.cost})
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
                digest=render(evidence),
                classification=_render_signals(classification),
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
        """Message the owner what the pass did.

        Best-effort: the edits are already durable and recorded, so a Telegram failure must not undo
        a good pass — but it IS logged loudly, because an unreported change is the thing this whole
        design is trying to avoid.
        """
        header = f"🌙 Ночная уборка · изменений: {run.changes} · разобрано сообщений: {run.owner_messages}"
        try:
            await self._bot.send_message(chat_id=conversation_id, text=f"{header}\n\n{run.report}")
        except Exception:  # noqa: BLE001 — the pass succeeded; only its delivery failed
            logger.warning("Curator report not delivered; changes stand and are in the run record", exc_info=True)


def _render_signals(classification: Classification) -> str:
    """The classification as one line per signal, quoting the owner so the curator can verify it."""
    if not classification.signals:
        return "(no owner messages classified in this window)"
    return "\n".join(
        f"Turn {s.turn_id} · {s.kind} · about: {s.about} · owner said: “{s.quote}”" for s in classification.signals
    )


async def ensure_indexes(database: AsyncDatabase) -> None:
    """Indexes for the curator's own collections. Idempotent; called at startup."""
    await CuratorRunStore.ensure_indexes(database)
    await RevisionLog.ensure_indexes(database)
