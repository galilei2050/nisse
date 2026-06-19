# IDEAS — patterns mined from reference assistants

A concept catalogue for nisse, distilled from four open-source assistants. Each
entry is the **idea** + a pointer to read more — NOT the full analysis. Read on
demand; do not load wholesale into context. `CLAUDE.md` is the authoritative
structure; this file is the "why / where to look deeper" companion.

> **This is how OTHERS did it — prior art, not a spec.** Nothing here is a decision
> for nisse and nothing must be copied. It's a menu and a map of known rakes: use it
> to avoid pitfalls others already hit, and to borrow an idea *only* when it earns its
> place. Verify each claim against the cited source before relying on it (the cites
> were cross-checked once, but code moves). Actual nisse decisions live in `CLAUDE.md`.

**Repos** — shallow clones live at `.references/<name>/` (gitignored; re-clone with
`git clone --depth 1 <url> .references/<name>` if missing):
- `hermes` — github.com/nousresearch/hermes-agent  (Python; closest agent-internals match)
- `openwebui` — github.com/open-webui/open-webui  (Python backend; mine backend only)
- `chatui` — github.com/huggingface/chat-ui  (TS; ideas, not code)
- `openclaw` — github.com/openclaw/openclaw  (TS; messaging-first, closest problem match)

Pointer format: _src:_ `repo: path/to/file` → resolves to `.references/repo/path/to/file`.

---

## Memory — three tiers (decided for nisse)

- **ShortTerm** — per-Turn working scratchpad. Lives during one `Assistant.reply()`,
  wiped when the reply is ready (today's in-memory ShortTermMemory accumulation).
- **LongTermHot** — small curated core, **always injected in every chat/turn** as a
  frozen snapshot in the volatile prompt tier. Durable in Mongo; curator keeps it tight.
- **LongTerm** — large durable fact store (Mongo + rebuildable vector index). NOT
  injected wholesale; surfaced by **Active Memory pre-reply recall**. Promotion
  LongTerm→LongTermHot is recall-gated (usage evidence), done by the curator.

Supporting patterns:
- **Dual-store** — durable DB = source of truth, vector index = disposable projection
  rebuilt from it; `add/replace/delete/list` by id (same ops for agent & curator).
  _src:_ `openwebui: backend/open_webui/models/memories.py`, `routers/memories.py`.
- **Memory as native tools** (add/replace/delete/list) — model decides when to persist.
  _src:_ `openwebui: backend/open_webui/tools/builtin.py`; `hermes: tools/memory_tool.py` (Hermes: add/replace/remove over fixed memory/user files — no `list`).
- **Frozen-snapshot + 3-tier-by-volatility prompt** (stable persona/overlay → context →
  volatile[facts+date]); snapshot once per run, writes durable-but-deferred to next run
  to preserve prompt cache; **date-only timestamp** (no minutes) or the cache dies.
  _src:_ `hermes: agent/system_prompt.py`, docs `prompt-assembly.md`.
- **Active Memory — pre-reply recall**: bounded recall sub-agent runs BEFORE the main
  reply, injects facts as **untrusted** context; hard timeout + circuit breaker; query
  modes `message`/`recent`/`full`; cheap model (Gemini Flash).
  _src:_ `openclaw: docs/concepts/active-memory.md`.
- **Recall-gated promotion** + temporal decay + MMR — promote short→long only after a
  fact is recalled ≥N times across ≥M queries above a score threshold; decay + MMR at read.
  _src:_ `openclaw: extensions/memory-core/src/short-term-promotion.ts`, `src/memory/{temporal-decay,mmr}.ts`.
- **Relevance-threshold on top-k** — drop below-threshold kNN hits so irrelevant facts
  aren't injected every turn. _src:_ `openwebui: routers/memories.py` (RELEVANCE_THRESHOLD).
- **Write-discipline as prompt guidance** — store declarative facts not imperatives;
  no task progress / ephemera; never harden tool-failures into permanent constraints.
  _src:_ `hermes: agent/prompt_builder.py` (MEMORY_GUIDANCE), `agent/background_review.py` ("do NOT capture" list).
- **Memory-context fencing** — wrap recalled memory in `<memory-context>`, scrub tags from
  model output, streaming scrubber across chunk boundaries. _src:_ `hermes: agent/memory_manager.py`.
- **Flush before compaction** — extract durable facts from full transcript before compacting.
  _src:_ `hermes: agent/conversation_compression.py`; `openclaw: docs/reference/session-management-compaction.md`.
- **Transcript recall via FTS, zero LLM cost** — `session_search` over SQLite FTS5 for
  "what did we decide last week". _src:_ `hermes: tools/session_search_tool.py`.

## Function calling / the loop

- **Native provider tool-calls at runtime** — never parse XML in the live path; Hermes-XML
  is only for training/trajectory serialization. _src:_ `hermes: agent/agent_runtime_helpers.py`.
- **Bounded loop** — hard cap on iterations (`for loop < 10`) as a fail-loud runaway guard.
  _src:_ `chatui: src/lib/server/textGeneration/mcp/runMcpFlow.ts`.
- **Parallel exec, re-ordered to call order, error fed back as `tool_result`** (don't swallow).
  _src:_ `chatui: .../mcp/toolInvocation.ts`. (baski.ToolSet already runs parallel.)
- **Think-block hygiene** — strip `<think>` from the assistant message resubmitted with tool_calls.
  _src:_ `chatui: .../mcp/runMcpFlow.ts`.
- **Tool-call repair** for weak/non-native models (promote text tool-calls → native). LATER.
  _src:_ `openclaw: packages/tool-call-repair/`.
- **Programmatic Tool Calling** — model writes a script calling tools via RPC; only stdout
  returns → collapses multi-step chains, intermediate results skip the context. LATER.
  _src:_ `hermes: tools/code_execution_tool.py`.

## Adding tools (ergonomics) — nisse = HYBRID (native baski.Tool + optional MCP)

- **docstring + type hints → schema** — write a typed Python function, get the model schema
  for free (no hand-written input_schema). _src:_ `openwebui: utils/tools.py` (convert_function_to_pydantic_model).
- **Self-registering registry + AST auto-discovery + `check_fn`** — drop a file, it's found;
  exposed only if its env/key exists. _src:_ `hermes: tools/registry.py`.
- **Register-vs-expose split** (toolsets + core) — registered globally, shown only if name is
  in the active toolset = always-on + loaded-per-skill + env-gating from one primitive.
  _src:_ `hermes: toolsets.py`.
- **Dunder param injection** — tool declares `__user__`/`__chat_id__`/`__event_emitter__`,
  framework binds via functools.partial and strips them from the model-facing schema.
  _src:_ `openwebui: utils/tools.py`.
- **Skills as data + progressive disclosure** — only a `<available_skills>` manifest (name+desc)
  in the prompt; body loaded on demand via one `view_skill(id)` tool. = our learned-skills + tiers.
  _src:_ `openwebui: models/skills.py` + `tools/builtin.py` (view_skill); `openclaw: skills/<name>/SKILL.md` frontmatter.
- **Self-loading tool = wiring is data, credentials are NOT** — the auto part is *schema/wiring*
  (API docs → baseURL + auth-method + endpoints → Mongo row → callable, no Python/redeploy). The
  *credential* (api-key, OAuth client, ToS, billing) is an identity/legal step you provision ONCE
  by hand — never automate it (security boundary). Config records *which* auth method + *which*
  secret to reference, not the secret. **Corollary:** self-loading fits simple REST + api-key/bearer
  only. OAuth/consent-flow services (Google) are too heavy for a config row → they stay **code skills
  (`tools/google/`)**, not self-loaded data tools. Don't try to auto-load Google.
- **MCP as optional tool source** — auto-discover external MCP server tools → schemas; sanitize
  names `^[a-zA-Z0-9_-]{1,64}$`, suffix on collision, cache list 60s, isolate per-server failures.
  _src:_ `chatui: src/lib/server/mcp/tools.ts`.
- **Central command registry → auto-derived Telegram BotCommand menu** (one source of truth).
  _src:_ `hermes: hermes_cli/commands.py`.
- **Valves/UserValves** — per-tool config as a Pydantic model, stored as data, UI/schema auto-derived. LATER.
  _src:_ `openwebui: models/functions.py`.

## Sessions — continue-vs-new-topic

- **Nobody does semantic topic detection.** All answer continue-vs-new by **time + identity**,
  not content. The proven recipe: one stable key per chat + idle/daily reset + `/new`.
  _src:_ `hermes: gateway/session.py` (SessionResetPolicy); `openclaw: src/config/sessions/reset-policy.ts`, `docs/concepts/session.md`.
- **Compaction in-place** when long (carry topic forward via `parent_session_id`), NOT a new topic.
  _src:_ `hermes: agent/conversation_compression.py`; `openclaw: docs/reference/session-management-compaction.md`.
- **Active path = computed slice, not raw log** (`ancestors`) — the seam where a boundary decision lives.
  _src:_ `chatui: src/lib/utils/tree/buildSubtree.ts`.
- **Title/tag prompt** — cosmetic in source, but repurposable as a cheap continue-vs-new classifier.
  _src:_ `openwebui: backend/open_webui/config.py` (tag-gen template).

## Self-improvement — which layers are realistically ours

Frozen model → `behaviour = f(system_prompt, tools, loaded_context)`. Self-improvement =
the agent writes to a persistent store that constructs the *next* agent. The agent stays a
stateless function; the "self" that improves is the store (Mongo), never the running code.
Layers, ranked by payoff/safety for nisse:

| Layer | What changes | Safe? | Where it lives |
|---|---|---|---|
| 1. Persistent memory (facts about the owner) | data (Mongo) | ✅ | `memory/` (LongTermHot always-injected, LongTerm via recall) |
| 2. Profile / preferences (CLAUDE.md pattern) | text in prompt | ✅ | curator prompt overlay → `assistant/prompt.py` |
| 3. Learned skills as data, progressive disclosure | data (Mongo) | ✅ | `toolset.py` loads specs; `view_skill` body on demand |
| 4. Self-loading tools (HTTP-service config rows) | data (Mongo) | ✅ | tool catalog by data, no redeploy |
| 5a. New code skills / `Tool` subclass | codebase (dev-time, Claude Code + review) | ✅ | `skills/<x>/`, git-versioned |
| 5b. Bot rewrites its own running code (DGM-grade) | executable code | ❌ don't | — sandbox+verifier+redeploy; not deployable for a personal bot |

Engine = the nightly **curator** (below), fed by `baski` traces (GCS + Mongo `traces`).
Ceiling: better at *our context / procedures / tool-selection*, not fundamentally smarter
(frozen model). Risk: drift / self-confirming loops → append-only versioning + rollback +
owner in the loop (already the curator's safety invariants).

## Self-improvement (curator)

- **Out-of-band background-review fork** — after a turn, a forked agent (cache-parity, memory/skill
  tools only) decides what to save/update; zero user-facing latency. The review **prompts** are the gem.
  _src:_ `hermes: agent/background_review.py`.
- **Curator safety invariants** — touch only `created_by:agent`, **archive not delete**, pinned exempt,
  snapshot-before-mutate, `rollback`. = our auto+rollback. _src:_ `hermes: agent/curator.py`, `curator_backup.py`.
- **Dreaming** — scored, gated, explainable promotion; keep `DREAMS.md` separate from durable `MEMORY.md`
  to avoid feedback loops. Copy the gating discipline, not the weights. _src:_ `openclaw: docs/concepts/dreaming.md`.
- **Iterative-update-the-prior-summary** — feed the curator the current overlay, ask it to revise not
  rewrite. _src:_ `hermes: agent/context_compressor.py` (_generate_summary — iterative-update prompt; orchestrated by `agent/conversation_compression.py`).

## Scheduling / self-invocation

- **DB-backed RRULE scheduler + atomic claim_due** (advance-then-execute) + run-history audit.
  _src:_ `openwebui: models/automations.py`, `utils/automations.py`.
- **Single pipeline reuse** — a scheduled run builds the SAME request object as a live message
  (zero behavioural drift). _src:_ `openwebui: utils/automations.py` (execute_automation).
- **Standing Orders (WHAT, in always-injected prompt) vs cron (WHEN, references them)** — schedules stay thin.
  _src:_ `openclaw: docs/automation/standing-orders.md`, `docs/automation/cron-jobs.md`.
- **Commitments** — hidden post-reply pass extracts *inferred* follow-ups ("interview tomorrow"),
  `maxPerDay`, delivered by a heartbeat. Makes the assistant feel alive. _src:_ `openclaw: docs/concepts/commitments.md`.
- **Isolated session for scheduled/background runs** — don't pollute the live thread's context.
  _src:_ `hermes: cron/`.

## Sub-agents

- **Agents-as-tools, leaf-role blocklist** (no delegate/memory/send), **summary-only** return to parent.
  _src:_ `hermes: tools/delegate_tool.py`.
- **Push/announce completion, never poll**; child output = **untrusted evidence**; cheaper model for children.
  _src:_ `openclaw: docs/tools/subagents.md`.
- **Keep shallow** — avoid manager-of-managers (explicit non-goal). _src:_ `openclaw: VISION.md`.

## Multi-provider / judge / eval

- **role→model_id indirection** — main / judge / curator / task each resolve to their own model via config.
  _src:_ `openwebui: utils/task.py` (get_task_model_id), `models/models.py` (registry-row-as-preset).
- **Feedback row = `{rating, model_id, reason, snapshot, tags}`** append-only — the join point for
  judge + offline evals + curator analytics; leaderboard/Elo are read-side functions over it.
  _src:_ `openwebui: models/feedbacks.py`, `routers/evaluations.py`.
- **Cheap task-model** for titles / intent classification / query-rewrite; reserve the big model for replies.
  _src:_ `openwebui: utils/task.py`.
- **Provider normalization** — one canonical wire format + thin bidirectional adapters.
  _src:_ `openwebui: utils/anthropic.py`.

## Telegram hardening (we are Telegram-only)

- **`extensions/telegram/`** is a master-class for a personal bot: durable ingress spool with crash-safe
  claim/lease, update-offset store, voice codec negotiation (voice-note vs audio file), sticker→vision,
  DM pairing allowlist. Most directly relevant code to read. _src:_ `openclaw: extensions/telegram/`.
- **keepAlive heartbeat** to keep Telegram "typing…" alive while a slow tool runs.
  _src:_ `chatui: src/lib/server/textGeneration/index.ts` (mergeAsyncGenerators).
- **event_emitter / event_call** — status edits ("transcribing… searching…") + inline-keyboard confirms;
  map the socket sink onto Telegram sendMessage/editMessageText. _src:_ `openwebui: socket/main.py`.

## Google Cloud Agent Platform (GCAP) — vendor prior art

The Gemini Enterprise Agent Platform (ex–Vertex AI Agent Engine). Read as a vendor's
*menu*, not a target — nisse already has its own spine (`baski.agents` loop, Mongo,
Cloud Run, Cloud Tasks/Scheduler, 3-tier memory + curator, Gemini judge). GCAP is
built for fleets of enterprise agents; nisse is one owner-only bot. Verdict per piece:

**Reuse (low lock-in, real value):**
- **Cloud Trace via OpenTelemetry** — the one clear win. OTel is a standard sink (no lock-in).
  Not free: it's re-instrumenting baski `TraceCollector` spans to OTel, then you get a trace
  viewer (vs only GCS+Mongo). ADK wires it with `--trace_to_cloud`. _src:_ docs `adk.dev/integrations/cloud-trace/`.
- **MCP as a tool source** — confirms our hybrid `baski.Tool` + MCP direction. No change.
- **Cloud Tasks / Cloud Scheduler** — Google's own agent-scheduling pattern *is* our
  `scheduling/router.py`. Already landed; confirmation only.

**Crib the design, NOT the service:**
- **Memory Bank** (managed long-term memory: LLM-extract → embed → kNN retrieve, scoped by
  **"memory topics"** + `user_id`). Overlaps our LongTerm + `recall.py` + curator. **Crib "memory
  topics"** as curator categories; don't adopt the service. _Verified mid-2026 (facts move fast — recheck):_
  it is **GA, billed since 2026-02-11 (~$0.25/1k events)** — NOT the 2025 preview. TTL exists
  (`ttl_hours`). Underlying memory-pipeline model is **not** publicly swappable (Google stack;
  your *agent* model can be Claude, the extractor can't). Memory-revisions / `revision_ttl` /
  direct non-session write-API: **unverified — no public docs found**; don't bank on them.
  The reject does NOT rest on those stale cons — see recall-index axis below. _src:_ docs `…/scale/memory-bank/setup`.
- **Recall index = Atlas, not a second system.** The dual-store projection (decided: "vector index
  = disposable projection rebuilt from truth") is a real role → three candidates: (a) **Atlas
  `$vectorSearch` + Atlas Search (kw/FTS)** — *winner*: projection lives beside truth in the one
  system we already run, rebuilt in place, curator stays sole writer; (b) Vertex AI Vector Search —
  a 2nd system holding a copy of truth; (c) Memory-Bank upload-mode — same 2nd-copy problem +
  partly-undocumented write path. Reject Memory-Bank-as-index because Atlas already does it, not "in principle".
- **Evaluation Service / Multi-Turn AutoRaters / Example Store** — trajectory-eval + autorater
  framing matches our `judge/` + `evals/`. Crib the multi-turn-rater concept; our Gemini judge is simpler.

**Explicitly NOT (enterprise-fleet machinery a single bot doesn't need):**
- **Agent Engine / Agent Runtime** (`reasoningEngines`) — proprietary non-self-hostable runtime; we run Cloud Run + baski.
- **ADK as a framework** — competes with `baski.agents`; its session/memory/tool patterns we already have. Crib, don't adopt.
- **Agent Gateway** — ingress/egress policy mesh for fleets; our owner-only `access.py` is the whole surface.
- **Agentspace / Agent Builder / Agent Studio** — low-code enterprise canvas; irrelevant to a code-first repo.
- **A2A protocol** — cross-vendor agent interop; our sub-agents are in-process. Revisit only if we expose/consume external agents.
- **VertexAISessionService / managed Sessions** — session store; Mongo + baski `ChatHistory` + time+identity reset already do this.
- **RAG Engine / Vertex AI Search / Vertex Vector Search** — single-user-overkill reject as openwebui RAG (below); the recall-index role is filled by Atlas (above), so even the bare Vector Search index is redundant.
- **Code Execution sandbox** — = the "Programmatic Tool Calling — LATER" note above; parked.

## Explicitly NOT copying

- openwebui RAG engine (BM25+vector+reranker, ~15 vector DBs) + multi-tenancy/permissions — overkill single-user.
- chatui LLM router (three hardcoded `if`s) — pick one strong model + one cheap task-model.
- Hermes platform breadth (terminal-backend zoo, Kanban, 8 memory plugins, ~60-param ctor) — take patterns, not platform.
- chatui word-slice truncation (`prompt.split(" ").slice(-N)`) — anti-pattern; do real token-budget compaction.
- `middleware.py` at 5k+ lines / heavy try/catch swallowing — mine ideas, not structure (conflicts with fail-fast).
