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

**Repos** (cloned to `/tmp/<name>*` during research — re-clone if gone):
- `hermes` — github.com/nousresearch/hermes-agent  (Python; closest agent-internals match)
- `openwebui` — github.com/open-webui/open-webui  (Python backend; mine backend only)
- `chatui` — github.com/huggingface/chat-ui  (TS; ideas, not code)
- `openclaw` — github.com/openclaw/openclaw  (TS; messaging-first, closest problem match)

Pointer format: _src:_ `repo: path/to/file`.

---

## Memory — three tiers (decided for nisse)

- **ShortTerm** — per-Turn working scratchpad. Lives during one `Assistant.reply()`,
  wiped when the reply is ready (today's in-memory KnowledgeTool accumulation).
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
  _src:_ `chatui: .../mcp/toolInvocation.ts`. (baski.ToolBox already runs parallel.)
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

## Explicitly NOT copying

- openwebui RAG engine (BM25+vector+reranker, ~15 vector DBs) + multi-tenancy/permissions — overkill single-user.
- chatui LLM router (three hardcoded `if`s) — pick one strong model + one cheap task-model.
- Hermes platform breadth (terminal-backend zoo, Kanban, 8 memory plugins, ~60-param ctor) — take patterns, not platform.
- chatui word-slice truncation (`prompt.split(" ").slice(-N)`) — anti-pattern; do real token-budget compaction.
- `middleware.py` at 5k+ lines / heavy try/catch swallowing — mine ideas, not structure (conflicts with fail-fast).
