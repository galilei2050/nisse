# Context pruning — why context bloats and how to keep it lean

Design note for the conversation-context strategy in `app/assistant/history.py`. Records the
investigation, the external research (with sources), the **RED-TEAM concerns**, and the chosen
pragmatic direction. Companion to `docs/history-test-cases.md`.

---

## 1. The problem, measured (not estimated)

On a bare "Ok" reply in prod, the model received **~42k input tokens**. Reconstructed exactly via
the Anthropic `count_tokens` API (reconstruction 42,869 vs. actual `cache_read` 42,225 — within 1.5%):

| Component | Tokens | Share |
|---|---|---|
| **Leftover conversation history (~70 turns)** | **34,111** | **81%** |
| Tool schemas (21 tools) | 5,872 | 14% |
| System prompt | 2,886 | 7% |

*(Absolute tokens are a dated measurement snapshot and will drift; the durable finding is the **ratio** — history dominates, schemas/system are minor.)*

**History dominates — not tool schemas.** (An earlier char/4 estimate was wrong: Cyrillic tokenizes
at ≈1.3 chars/token, so Russian history is ~3× heavier than a naive char count suggests.)

### Why history accumulates
- `drop_tool_turns()` only removes turns with **no text**. Any turn carrying assistant text is kept
  forever. So all conversational back-and-forth (incl. disposable chatter) piles up.
- `truncate()` exists and is token-based, but its threshold sat **above any context the conversation
  actually reached** (the owner notices bloat first), so it never fired. No deterministic mechanism
  trimmed a long *text* conversation.

### Why the model doesn't self-clean (prod traces, 11 consecutive turns)
The model prunes **only after explicit, repeated kicks**:
- 9 normal turns: prune = 0; context climbs 39k → 43k monotonically.
- Explicit "save and clear old messages" → it pruned only **14** turns (its current working set).
- Full cleanup happened only after two direct accusations ("Why didn't you delete those 63?" → 37,
  "Why did the rest stay?" → 33), dropping context to 14k.

Root cause of the laziness: the only prune instruction (baski `DeleteMessagesTool.system_prompt`) is
scoped to *"prune the source turns you just saved"* — a narrow post-`working_note` cleanup, not
"keep the whole transcript small." And `prune_transcript` requires enumerating explicit `turn_ids`,
so the model satisfices and lists a handful, never 60.

---

## 2. Usage model (this is a single-owner PERSONAL assistant)

The full scenario taxonomy lives in **`docs/usage-scenarios.md`** (16 life domains, every scenario
tagged by retention need, grounded in published usage data). Summary of what it says for pruning:

Every turn falls into one of five **retention classes**:
- **DISPOSABLE** (~40%) — value consumed in the turn; nothing to keep (smalltalk, lookups, "сколько
  времени", venting). *Venting is one member here, not the whole story.*
- **ARTIFACT** (~20%) — durable result lives outside the transcript (reminder, reservation, list).
- **DURABLE-FACT** (~15%) — a lasting fact/preference; extract to memory, then drop the chatter.
- **KEEP-WHILE-ACTIVE** (~15%) — verbatim continuity needed *during* the session only.
- **REFERENCE-LATER** (~10%) — may be explicitly recalled (research result, decision + rationale).

**~75% of turns can leave working context almost immediately** once their durable residue (if any) is
saved; only KEEP-WHILE-ACTIVE + REFERENCE-LATER (~25%) hold real text, and KEEP-WHILE-ACTIVE is
short-lived. Published priors corroborate this: ~80–90% of general-assistant usage is instrumental,
short-session, low-memory; only ~10% is memory-dependent (`docs/usage-scenarios.md` §4). The two
classes a pruner must NOT drop: KEEP-WHILE-ACTIVE *mid-session*, and a DURABLE-FACT not yet saved.

---

## 3. External research (Perplexity consensus pairs)

Two same-prompt agent pairs (one source = Perplexity) — triggers, and production mechanisms.

### Consensus on *when* to prune
- **No single signal is sufficient** — compose several.
- **Compaction-to-memory beats deletion** — evicted content should remain retrievable, not destroyed.
- **Reliable triggers:** token budget; topic-shift (embedding drift); task/goal completion;
  coreference/anaphora boundary (use it to *protect* still-referenced turns); explicit user commands
  (`/clear`, "forget this").
- **Unreliable in isolation:** pure recency window with no archive; timeout as hard reset;
  uncalibrated topic segmentation; blind summarization; the LLM self-assessing what's important.

### Consensus on production mechanisms and their documented failure modes
| Approach | Mechanism | Documented failure mode |
|---|---|---|
| Sliding window | keep last N, drop older | irreversible loss; "lost in the middle" |
| Progressive summarization | fold old turns into a running summary | **summary drift**, hallucinated/over-general summaries, detail loss, +1 LLM call/turn |
| Hierarchical paging (MemGPT/Letta) | page in/out of an external store | retrieval/paging mistakes, stale structured state, complexity, opacity |
| RAG-over-history | embed turns, retrieve top-k | retrieval misses, context pollution, temporal confusion |
| Persistent fact-extraction (ChatGPT/Claude memory) | distill durable facts | wrong/stale facts, non-enforcement (probabilistic, not DB-like) |
| Anthropic context-editing / compaction | clear oldest tool-results/thinking, summarize rest | mis-clearing, summary drift, **client↔model desync**, cache invalidation |

Two recurring failure clusters everywhere: **(1) compression** (summary drift / detail loss) and
**(2) selection** (retrieval miss / pollution / lost-in-the-middle), plus stale facts and
client↔model divergence. Anthropic's pattern — clear oldest tool/thinking **after** the memory tool
saves key facts — is the direct analogue for nisse.

---

## 4. Candidate strategies considered

- **S1 — Deterministic window:** keep last N text turns verbatim; older soft-deleted from context
  (recoverable in Mongo). Pure code, no model, no summary, no retrieval.
- **S2 — Token-budget compaction:** when history > threshold, an LLM summarizes the oldest block.
- **S3 — Topic-shift archive:** embedding drift detects a topic change → extract facts → drop the
  prior topic's turns.
- **S4 — Hybrid multi-signal:** last-K verbatim + token trigger + compaction to memory + coreference
  protection + `/clear` override + archive-all. (The "correct" consensus design.)

---

## 5. RED-TEAM concerns (keep these — they are the guardrails)

Independent agent pairs stress-tested each strategy against the usage hypotheses. **Both pairs
converged** per strategy.

### S1 (deterministic window) — concerns
- **ROOT FLAW: the save-then-drop race.** S1's correctness assumes every still-useful fact was
  already copied to long-term memory — but the only thing that copies is the model, probabilistically,
  during the very turns scrolling out. Eviction is deterministic; the compensating save is **not**.
  A deferred or silently-failed save → the turn is evicted → permanent silent loss.
- **DANGEROUS for:** long emotional/single-topic threads (refs 30 turns back), early constraints
  needed later, multi-day project resume, tool-result recall ("book the 2nd flight you found").
- **SAFE for:** short, transactional, self-contained Q&A.
- **Keystone fix:** eviction must **lag a confirmed save** — never drop a turn in the cycle it
  becomes evictable; archived turns must stay **recoverable**.
  → *nisse already soft-deletes pruned turns to Mongo (recoverable) and has long-term memory, so the
  loss is bounded — but recovery is manual (human), not automatic (model).*
- **Mitigating counter-fact (owner):** for DISPOSABLE turns there is **nothing worth saving**, so the
  save-then-drop race does not apply to disposable chatter — exactly the bulk of the load.

### S3 (topic-shift archive) — concerns → judged a "spaceship" and it fails the headline case
- **HEADLINE FAILURE:** in a long *single-topic* session no boundary ever fires → S3 **never prunes**
  → useless exactly when bloat is worst. It needs a hard turn/token **ceiling** backstop anyway.
- **Ping-pong thrash:** interleaved topics → boundary every turn → extract/drop/re-fetch churn,
  doubled latency/cost, the active thread flickers out.
- **False mid-task boundary:** sub-topic drift within one task (flights→neighborhoods→restaurants)
  crosses the threshold → drops context the next step needs → "assistant has dementia mid-task."
- **Digression premature-archive:** "btw what time is it?" mid-conversation fires a boundary →
  the deep topic gets archived → owner returns and it's gone. A 1-turn digression evicts a 30-turn topic.
- **Hot-path cost + uncalibrated threshold:** an embedding call per turn on the reply path; one
  magic threshold can't serve both a 40-turn grief session and dinner/work ping-pong; ~zero data to
  tune it on; Cyrillic embedding quality unvalidated.
- **Extraction drift:** the boundary fact-extraction is itself an LLM summary call (same drift/miss
  risk), and dropping the source immediately makes any miss permanent.
- **Verdict:** boundary detection is at best a *hint*, never the sole control. Too complex for nisse now.

### S2 / S4 — deferred
Summarization (S2) carries summary drift + per-turn cost; the full hybrid (S4) adds coreference
protection and RAG-recall — real value at scale, but over-engineered for one owner at this stage.

---

## 6. Decision (pragmatic — "не космолёт")

The **real, observed** problem is the DISPOSABLE class accumulating across sessions: tool-less text the
current code keeps forever. That wants **deterministic dropping**, no summarization, no topic model.

**Decision — deterministic budget:** lower the `_MAX_TOKENS` budget in `history.py` (it was set far
above any reached context, so it never fired) until the **existing** trimming actually bites — when
effective input nears the budget it drops the oldest turns. It is already token-based (counts the
whole cached prefix via `effective_input_tokens`), already operates on **all** turns, and already
soft-deletes to Mongo (recoverable → not destructive); `drop_tool_turns()` keeps removing pure-tool
turns each reply. (Current budget value: see `_MAX_TOKENS` in the code.)

**Where it fires matters as much as when.** Trimming runs **once per reply** (`compact()`, called from
`Conversation.reply` after the answer is delivered), never inside the agent loop. baski reports usage
after every API call, and dropping a turn there costs twice over:

- *Quality* — it cuts context out from under a reply that is still being composed. Observed in
  production traces (dated snapshot, Aug 2026): single replies whose transcript went 56 → 47 → 15
  and 60 → 48 → 41 → 35 messages while the model was answering.
- *Money* — it moves the head of the message list, so the cached prefix stops matching and the whole
  transcript is re-written at the 1.25x write rate instead of read back at 0.1x. 22 of 27 full-prefix
  breaks followed an over-budget call; the rewrites were ~13% of the period's API spend.

Trimming after the answer keeps the reply whole and reacts to the freshest measurement — the run that
just finished — instead of the previous one's.

**Tool data before words.** Age is the wrong axis to cut on: the budget was being spent on payloads
while the conversation itself was evicted. Measured on the owner's transcript (dated snapshot, Aug
2026): of everything already pushed out of context, **42% was base64 images/PDFs, 30% tool results,
6% reasoning, 5% tool arguments — and only 16% was actual conversation text**; of what was still IN
context, 45% was tool payloads. So an over-budget reply first sheds machinery from turns older than
`_PAYLOAD_RETENTION` (`_reduce_old_turns_to_text`), and only a reply that is *still* over budget drops
whole turns. At the same budget this roughly **2.5×** the window (29 turns / 3.4h → 57 turns / 8.5h,
simulated on the real transcript). Days of memory need a bigger budget, not a better filter.

The retention window exists for follow-ups: "show me the second one you found" reaches back into the
tool output of the exchange it follows. 63% of the owner's messages arrive within 5 minutes of the
previous one and 85% within an hour (1,007 gaps, Jun–Aug 2026), so an hour of payloads covers the
follow-up case while everything older sheds.

**Decision — cheap manual lever:** `prune_transcript` takes a `keep_last=N` param (baski
`DeleteMessagesTool`) — "keep only the last N turns" in one call instead of enumerating ids — for the
model's *optional* deliberate cleanup on a topic change. It is no longer the safety net; the
deterministic budget is.

**Durable facts:** unchanged — the model saves to long-term / core memory the moment a real fact
appears. For DISPOSABLE turns it saves nothing, which is correct.

**Defer (spaceship):** S2 summarization, S3 topic detection, S4 hybrid/coreference/RAG-recall.

### Usage-hypothesis → pros / risks
| Hypothesis | Behavior | Pro | Risk |
|---|---|---|---|
| **DISPOSABLE** (~40%) | old tail dropped by window; nothing extracted | kills the main bloat source; zero extra LLM calls | none — nothing worth saving |
| **ARTIFACT (~20%) + cross-session trivia** | same window vacuums trivial turns | removes the cross-session bloat that caused the complaint | none — artifact persists elsewhere |
| **KEEP-WHILE-ACTIVE** (~15%, long session) | last K turns kept; if the session exceeds budget, earliest turns trim | keeps the active thread; not a spaceship | loses early **verbatim** of a very long single session — mostly DISPOSABLE within it; durable facts already in memory. **Residual risk, accepted.** |
| **DURABLE-FACT** (~15%) | belongs in long-term/core memory (existing design) | survives any trim if saved | if it lived only in the transcript and got trimmed → lost. Mitigation: generous budget + it's memory's job |
| **REFERENCE-LATER** (~10%) | compact recall handle survives in memory | research/decisions recallable | raw transcript evicted — acceptable if the conclusion was saved |

### Carry-over guardrails from the red team
- **Bound the loss:** archived turns must remain soft-deleted-recoverable in Mongo (already true) —
  trimming the *active prompt* must never mean *destroying* the record.
- **Don't outrun the save:** keep the window generous enough that a just-stated constraint isn't
  evicted before the model can persist it; durable facts are memory's job, not the transcript's.
- **No silent failure:** log every trim (token count, turns removed) so bloat regressions are visible.

---

## 7. Code touch-points
| File | What |
|---|---|
| `app/assistant/history.py` | `_MAX_TOKENS` is the context budget; `truncate()` records each call's size, `compact()` does the shrinking, `_forget()` is the only way a turn leaves. |
| `app/assistant/conversation.py` | calls `compact()` once per reply — the only place the transcript is allowed to shrink. |
| baski `DeleteMessagesTool` (`delete_messages.py`) | `keep_last=N` — keep only the last N turns, drop the rest in one call (`turn_ids` still supported). |
| `tests/assistant/test_history.py` | covers `keep_last` durability and budget-driven truncation. |

The one tunable is the **context budget** (`_MAX_TOKENS`): smaller = cheaper, larger = longer active
thread held verbatim.

---

## 8. Sources

**Verified / well-known (high confidence):**
- MemGPT — Packer et al., arXiv 2310.08560.
- Lost in the Middle — Liu et al., arXiv 2307.03172.
- TextTiling — Hearst, *Computational Linguistics* 1997 (ACL J97-1003).
- Generative Agents — Park et al., arXiv **2304.03442** (research pass mis-cited as 2404.00573 — corrected here).
- LangChain memory classes — `ConversationBufferWindowMemory`, `ConversationSummaryMemory`,
  `ConversationSummaryBufferMemory`, `ConversationVectorStoreTokenBufferMemory` (LangChain docs).
- Anthropic — context-editing (`clear_tool_uses_20250919`, `clear_thinking_20251015`) and compaction
  docs; Claude Code memory & `/compact`.
- OpenAI — Memory FAQ; Claude Projects support docs.
- Letta / MemGPT successor — github.com/letta-ai/letta.

**As surfaced by the Perplexity research pass — mechanism descriptions solid, exact IDs not
independently verified (treat with caution):** Mem0 chat-history summarization guide; recursive
dialogue summarization (reported arXiv 2308.15022); DialSTART (reported arXiv 2305.02747);
dual-process topic-shift (reported COLING 2025); embedding-TextTiling session segmentation
(Interspeech 2016); dialogue anaphora resolution (ACL W04-2310); Apple CREAD; TOD survey; Oracle
agent-memory guide; Redis context-pruning; summary-drift engineering writeups.

**Likely hallucinated by the research pass — DO NOT trust without checking:** "DyCP, arXiv 2601.07994"
and a "long-term-memory benchmark, arXiv 2604.20006" (future-dated IDs). Their *claims* (a hard
ceiling beats boundary-only pruning; recallable > dropped) are corroborated by the verified sources
above, so the design does not depend on them.

> Provenance: investigation scratch in `scratch/context-pruning-brainstorm.md` (git-ignored);
> token breakdown reproducible via `scratch/count_tokens_verify.py`.
