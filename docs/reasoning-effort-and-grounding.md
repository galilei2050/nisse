# Reasoning effort & grounding — how nisse should size and ground its answers

How to manage the agent's "thinking" parameters and its decision to gather data, grounded in an
empirical experiment, the latest prod traces, and the delegation/trust literature. Numbers here are a
**dated snapshot (2026-06-28)** kept as *evidence of research* — the durable takeaway is the
direction/ratio, not the absolute value or current config (read the code for current values).

## Decision (the durable conclusion)

1. **Effort is a static parameter, not routed per request.** `thinking = {adaptive, display:
   summarized}`, one flat `effort` (`high`). Per-request effort routing — via a classifier or an in-loop
   tool — was tested and rejected: it doesn't change the outcome that matters and adds cost/latency.
2. **The lever that matters is the GROUNDING decision**, not effort. The real failure is the bot
   answering high-stakes analytical questions *from memory* instead of gathering data. Fixed with a
   system-prompt rule (the "Ground analysis" clause in `NISSE_SYSTEM_PROMPT`, `app/assistant/assistant.py`),
   **not** with any parameter/classifier/tool machinery.
3. **Pair grounding with an honest signal** — the answer states whether it was grounded ("Источники: …")
   or quick-from-memory ("заземлить на данных?"). This is the research-backed mechanism that keeps
   delegation worthwhile (turns total re-checking into cheap spot-checks).

This composes with the [Decision principles](../CLAUDE.md#decision-principles).

## Why effort routing was rejected (experiment, dated snapshot)

Four configurations were run end-to-end through `make probe` (real API + Mongo + tools) over a
difficulty gradient (trivial CRUD → deep research/planning):

| Arm | effort policy | verdict |
|---|---|---|
| Baseline | static `high` (current) | trivial already cheap; hard research adequate but **variable** |
| Flat `medium` | static `medium` | ≈ baseline; marginally less thinking on chatty turns; no quality loss |
| In-loop tool | start `low`, agent self-escalates via an `assess_difficulty` tool | **worst** — skipped on 3/4 cases → `low` floor under-thinks; conservative self-rating; busts the prompt cache; priciest |
| Sonnet pre-classifier | a fast model picks effort up front | tiers reliably, but taxes **every** message with a round-trip (latency + ~$0.003); the depth it "produced" was variance, not the classifier |

**The decisive finding — research depth is variance-dominated, not effort-driven.** The same comparison
request ("сравни 3 робота-пылесоса"), run 3× at the *same* `high` effort, produced shallow / deep / deep
(the shallow one was a 1-of-3 outlier). A `medium` run out-researched a `high` run. So effort level does
not determine how deeply the agent researches — per-run adaptive variance does. No effort mechanism
fixes that, so routing effort buys nothing for a single-user bot (and adds machinery →
[no-machinery-without-amortization](../CLAUDE.md#decision-principles)).

Trivial cost is already solved by adaptive thinking: "add milk" costs ~the same tiny amount at `high`,
`medium`, or `low` — adaptive skips thinking on trivial regardless of effort.

## The real problem (latest prod traces)

Ticket-resale thread (2026-06-27/28): on data-demanding, high-stakes, **unverifiable** asks
(«сделай бизнес-модель, основанную на данных») the bot **one-shots from memory** (`turns=1, tools=[]`),
and the owner repeatedly pushes back («Старайся лучше», «Давай ещё раз»). Trivial/logistics asks are
fine. The failure is **the grounding decision** (research vs answer-from-memory), and it is
*unpredictable* — the worst profile for trust (intermittent failure beats consistent failure for
eroding it).

Reproduced locally: even with the "verify, don't guess" rule, a held-out business decision
(«стоит ли открывать кофейню?») grounded only **0/2** — answered from memory.

## The grounding rule, and its empirical validation (dated snapshot)

The rule (in `NISSE_SYSTEM_PROMPT`): for a task that builds a model / compares / recommends / estimates /
analyses anything with real-world quantities, gather data with tools **before** answering, never from
memory; for comparisons pull more candidates than presented; close with an honest source line, or — if
not grounded — offer the deeper pass. Effort left static; no classifier, no tool.

Metric: **grounding-rate** = fraction of runs on a high-class request that call ≥1 research tool. Tested
on **held-out** cases phrased outside the rule's enumerated words (anti-overfit), plus trivial controls:

| Case (held-out) | old prompt | with rule |
|---|---|---|
| «стоит ли открывать кофейню?» | **0/2 grounded** (one-shot) | **3/3 grounded** + signal «Источники: …» |
| «стоит ли покупать франшизу шаурмы?» | — | **2/2** (4 tools + skeptical cross-check of a review vs marketing) |
| «выгодно ли сдавать квартиру посуточно?» | — | **2/2** — caught a memory-answer would miss: *Airbnb not operating in RU since 2022* |
| «окупится ли солнечная панель за 10 лет?» | 3/3 | 1/1 (no regression) |
| trivial «добавь молоко» / «напомни» | — | domain tool only — **not over-grounded** |
| missing-info «bar между мной и Belmont» | — | **asks for location**, doesn't fabricate or over-search |

**Result:** across held-out business-decision cases the rule grounds **7/7** vs the baseline's **0/2** on
the same coffee case — generalises beyond its literal wording, adds the honest signal, and breaks neither
trivial answers nor the "ask when the owner's input is missing" boundary. A prompt rule alone was
sufficient for the grounding failure; satisfies the "add to the prompt only with evidence it changes
behaviour" bar.

Open: this validates the *binary* grounding decision (research vs memory), not the separate
research-*breadth* variance (3 vs 5 candidates at the same effort), which remains adaptive run-to-run.

## Trust/delegation grounding (why these criteria)

Why "reliability > peak", "weight by stakes × unverifiability", and "honest signaling" are the criteria —
strong cross-disciplinary consensus (two independent Perplexity passes):

- **Principal-agent** (Holmström 1979, Jensen-Meckling 1976, Townsend 1979, Diamond 1984): delegation
  pays only if you don't verify every output; once verification cost ≥ (owner's cost − agent's cost) it's
  worthless. Optimal = selective spot-checks, never 100%.
- **Trust-in-automation** (Lee & See 2004, Yang/Wickens 2016, Hoff-Bashir 2015): trust builds slowly,
  collapses fast (a valid recommendation +~2% vs an invalid one −~5%); one lapse generalises to the whole
  agent; intermittent failure worse than consistent.
- **Algorithm aversion** (Dietvorst 2015/2018): people abandon an erring algorithm even when it beats
  humans on average; machine errors punished harder; mitigated by user control + transparency.
- **Variance vs mean** (Ellsberg, betrayal aversion): under costly verification, a reliable good-enough
  delegate beats a brilliant-but-erratic one — the low-quality tail hides undetected misses.
- **Trust calibration**: silent variable quality is the trust-killer; an honest "not fully verified" +
  a path forward restores cheap targeted spot-checking. (Caveat: LLM self-confidence is only roughly
  calibrated.)

## How this was measured (tooling)

`make probe MSG="…" [U=<id>]` = **run** one case end-to-end and persist the full trace to
`scratch/traces/`. **Trace analysis is a separate step** — `.claude/skills/analyze-traces/summarize.py`
(per-turn thinking size, tool calls) / `show_text.py` (full answer). Use a unique conversation_id per run
(reused ids leak history → the agent answers from the prior run, not fresh). See [[verify-via-make-probe]].
