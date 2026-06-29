# LLM-as-judge for "didn't finish to the end" — experiment working doc

**Status: hypothesis + two validated failure cases. Judge solution NOT yet validated** (method below;
results section is empty until `make probe` runs). Numbers are a **dated snapshot (2026-06-28)** kept as
evidence — read the code for current behaviour.

Companion to [reasoning-effort-and-grounding.md](reasoning-effort-and-grounding.md): that doc fixed the
*grounding* decision (research vs answer-from-memory) with a prompt rule. This doc targets a **different,
remaining** failure it explicitly left open — the agent does the research but **stops one step short of
the requested artifact**, and ends with a question instead of finishing.

## The problem: finish-the-work, not grounding

After the grounding rule shipped, the agent now gathers data — but on multi-part / artifact asks it
delivers *part*, silently drops sub-deliverables, and closes with "want me to…?" instead of the result.
The owner then has to push ("Старайся лучше", "давай ещё раз"). **That push is the thing to delete** —
the owner can only judge what he already knows, so for delegated work he is a broken judge; making him
re-prompt defeats delegation.

Crucially this failure is **checkable from the transcript without domain knowledge**: "is there a PnL
table?", "are there booking links?", "did it end with an artifact or a question?". That is what makes an
automated judge viable here (unlike judging *truth*, which needs the answer).

## Two validated cases (empirical, current agent, 2026-06-28)

Both run through `make probe` against current code (grounding rule live). Both finish the research and
stop short with a question — the shared tell.

### Case A — from owner's real prod prompts (ticket resale) · weight: HIGH

Real thread where the owner hit "Старайся лучше / давай ещё раз" three times (prod traces
`5b651a14`, `387d3535`, `81506b63`). Self-contained probe form:

> «Перепродажа билетов выглядит как настоящий бизнес. Подумай очень хорошо и сделай простую
> бизнес-модель, основанную на данных — с расчётом **PnL и юнит-экономики**.»

- **Finish bar:** grounded numbers → assembled **PnL** (revenue, COGS, fees, opex, net) + **unit
  economics** per deal.
- **Current agent** (probe U=70001, trace `b13bd6f1`, 4 turns / 7 tools / ~$0.19): ✅ grounded (market
  $9.8–13B, broker case, fee schedules, "Источники: …") — grounding rule works. ❌ **never built the
  PnL / unit-economics model** (gave industry economics instead). ❌ ended with a **question**: "are you
  modeling this to start, or analyzing the industry? I can sharpen…".
- **Verdict:** research done, **artifact not delivered**, decision pushed back to owner.

### Case B — web-derived realistic multi-deliverable (trip planning) · weight: MEDIUM

> «Спланируй романтические выходные для двоих недалеко от SF, бюджет до 800 долларов. План по дням
> **с временами**, конкретными местами, **актуальными ценами** и **ссылками где бронировать**.
> В конце — **итоговая смета**.»

- **Finish bar:** 5 sub-deliverables — day-by-day **with times**, specific places, **current prices**,
  **booking links**, final budget tally.
- **Current agent** (probe U=70002, trace `c543f21f`, 4 turns / 8 tools / ~$0.14): ✅ specific places,
  ✅ budget tally ~$750–800. ❌ **zero booking links** (explicit ask). ❌ **no times** ("late-morning"
  not clock times). ⚠️ prices partly guessed as ranges, not verified. ❌ ended **asking a question** —
  literally "Want me to— actually, I'll just do it: tell me if you'd rather…" (plus a text-corruption
  artifact "2нд night").
- **Verdict:** ⅗ sub-deliverables; two dropped silently; closes with a question.

## Hypothesis: a cross-family LLM judge catches these and triggers an internal retry

**Claim:** a separate, cheap, **different-family** model that reads the transcript and checks
*completeness of process* (not truth) can detect both failures and loop the executor automatically —
removing the owner's "Старайся лучше". Grounded in the `/goal` builder-evaluator pattern (the model
doing the work is the worst judge of whether it's done) and the trust research (an unverifiable miss is
disproportionately costly; intermittent failure erodes trust fastest — see
[reasoning-effort-and-grounding.md](reasoning-effort-and-grounding.md) §trust).

Why this is the right lever (vs effort / prompt, already tried):
- **Effort** doesn't change depth (variance-dominated — that doc's finding). A judge-**retry** resamples
  the bad tail instead of trying to prevent it.
- **Prompt alone** fixed grounding but these cases prove it doesn't reliably enforce *completeness*; the
  agent still self-declares done.

Design (guardrails, not a veto):
- **Judge checks a concrete checklist** extracted from the request, transcript-verifiable: are all named
  sub-deliverables present? did it end with the artifact, not a question? — NOT "is the answer correct".
- **Cross-family judge** (e.g. Gemini Flash 3.1) for **decorrelated blind spots** — a same-family judge
  (Haiku) shares Opus's failure modes and waves through the same misses. (Capability is also fine: Flash
  3.x benches strong on LLM-as-judge / instruction-following; Haiku has no comparable published numbers.
  Dated snapshot.)
- **Retry cap** (1–2), then deliver with an honest "couldn't fully finish X" note — escape valve so it
  never spins (the documented `/goal` failure on vague criteria).
- **Fires only on the high-stakes / multi-deliverable class**, not "добавь молоко" — trivial passes
  instantly. Respects [no-machinery-without-amortization](../CLAUDE.md#decision-principles): the guard is
  on the costly-and-unverifiable quadrant, not every message.

Open risk: LLM self/peer-judgment is only roughly calibrated → false "not done" burns a retry; false
"done" misses. The cap + honest fallback bound the downside.

## How to validate (next — not yet run)

Metric: **finish-rate** = fraction of runs where every requested sub-deliverable is present and the reply
ends with the artifact (not a question). Transcript-checkable, so the judge's own criterion = the metric.

1. Baseline: run Case A & B N× each through `make probe`, score finish-rate (expect low — both fail now).
2. Prototype the judge as a **separate cross-family call** on the transcript; on "not done" feed its
   checklist back and re-run the executor (cap 2).
3. Compare finish-rate baseline vs with-judge, and record added cost/latency. Accept only if the tail
   lifts materially at acceptable cost; else reject like effort-routing.
4. Decide separate-model vs same-Opus-second-pass empirically (start cross-family for independence).

See [[verify-via-make-probe]] — `make probe` runs a case, trace analysis is separate
(`.claude/skills/analyze-traces/`). Tooling added for this: `.claude/skills/web-fetch/` (keyless
search/fetch for sourcing realistic cases).

## Results (prototype, 2026-06-28, dated snapshot)

Prototype: Gemini `gemini-3-flash-preview` (cross-family) grades completeness from
`(request, answer)`; on `finished=false` its feedback is fed back as a synthetic user turn and the
executor re-runs (cap 2). Wired into `Conversation.reply`, tested via `make probe`. 4 runs (each case ×2).

**Judge accuracy — no false passes, no false flags (small N):**
- Case A ×2 → both `finished=true` on attempt 1. Verified by reading the answers: this run-variance landed
  *deep* — the executor actually built the full PnL (unit-economics table + annual scenario + sensitivity,
  grounded, sources). Judge correctly passed complete work. (Contrast the baseline trace `b13bd6f1`, which
  ended with a question — Case A is *variable*, matching the variance finding.)
- Case B ×2 → both `finished=false` on attempt 1, with missing-lists that **matched the manual analysis
  exactly**: "clock times", "direct booking links", "Sunday schedule", "specific hotel not a range".

**Retry efficacy:**
- **Case B run 4: `false → (1 retry) → true`.** Clean close within cap.
- **Case B run 3: `false → false → (cap)`,** but completeness rose monotonically — attempt-1 gaps (4 major:
  no times, no links, no Sunday, vague hotel) → attempt-2 gaps (3 minor) → final answer had clock times
  both days, **booking links + phone numbers**, specific hotel ($310 w/ url), budget tally ~$715 + cheaper
  alt, and a grounded correction the baseline missed (*Arata's farm is fall-only, closed in June → swapped
  for a year-round trail*). The internal loop reproduced the owner's manual "Старайся лучше".

**Finish-rate:** baseline (attempt-1 = no judge) Case A 2/2, **Case B 0/2** (0/3 incl. baseline trace).
With judge-retry: Case B → 1/2 fully closed + 1/2 materially improved (capped). Case A unaffected
(already complete; judge added one no-op check ~$0.002).

**Cost & latency (the real tradeoff):**
- Judge call: ~**6–9s warm** (13.6s cold — per-call client construction; a shared client would cut this),
  ~**$0.002** each. Cheap and fast, as predicted.
- The cost is the **extra executor passes**, not the judge. A turn that triggers 2 retries runs the Opus
  executor 3× → roughly **2.5–3× cost and latency** vs a single pass. (Note: the prototype's printed
  `cost=$` reads `result.total_cost` of the *last* `execute()` only — it undercounts; true cost is the sum
  of all passes + judge calls.) Justified only on the high-stakes/multi-deliverable class, where the owner
  would otherwise re-prompt manually anyway — exactly the amortization rule.

**Limitation surfaced — judge can demand what the executor's tools can't produce.** Run 3 capped on
*booking links*: SerpAPI returns search hits, not guaranteed-bookable URLs, so the executor can't always
satisfy that criterion and the loop would spin to the cap. **Implication for a real impl:** the cap must
end in an **honest fallback** ("couldn't get live booking links — here's the plan + search links"), not a
silent stop; and the judge's checklist should weight *what the tools can deliver*. One attempt-2 flag was
also slightly harsh (final was near-complete) — LLM judge calibration is rough, as flagged.

**Direction: confirmed.** A cross-family completeness judge reliably catches the finish-the-work failure
that prompt/effort don't, and the retry closes or materially improves it — removing the owner's manual
push. Worth building properly, scoped to high-stakes turns, with an honest-fallback cap.

### If productionised (not in this prototype)

Build the planned `app/judge/` (provider-agnostic `Judge` + Gemini impl + rubric, per `app/CLAUDE.md`),
with: a **shared genai client** (not per-call), the retry **not polluting durable history** with synthetic
turns, an **honest-fallback** at the cap, and a **gate** so it fires only on the high-stakes/multi-
deliverable class (trivial turns skip the judge entirely). Open: tune the cap, and decide the gate
(executor self-tags stakes vs a cheap classifier).

