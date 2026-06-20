# nisse backend — agent-centric Telegram assistant

One LLM agent loop is the core. Telegram is a thin I/O skin: a voice/text
message comes in, gets transcribed, goes into `Assistant.reply()`, the agent
decides which tools/sub-agents to call, the reply goes back out. Everything
else (memory, scheduling, search, Google Suite) is a **tool** the agent calls.

Built on `baski`. The agent framework (`Agent` / `Tool` / `ToolSet` /
`MessageHistory` / `ShortTermMemory` / `TraceCollector`) is ported from
clarity-auto-care into `baski.agents` — import from there, do not vendor.

**`Agent` (baski) is abstract; `Assistant` (this app) is concrete.** `baski.agents.Agent`
is a transport-agnostic LLM loop — it knows tools, messages, and tracing, and
nothing about Telegram, this user, or this product. All the concrete knowledge
lives in `Assistant`: it knows it speaks through Telegram, who the owner is, which
tools/skills are wired, and what the system prompt says. Keep that line clean —
no Telegram type, chat id, or aiogram import ever reaches `baski`; conversely
`Assistant` never reimplements the agent loop. If a feature is generic, it belongs
in `baski.agents`; if it's "because we're a Telegram assistant for this user", it
belongs in `Assistant`.

Concepts, rationale, and source pointers mined from reference projects live in
`IDEAS.md` — not here. This file is project structure only.

## Layout

```
app/
  backend.py        NisseBot(TelegramServer): wiring only — routers() + middlewares()
  access.py         AllowlistMiddleware — owner-only gate (outer middleware)

  shared/           cross-domain code — no domain logic of its own
    db.py           Mongo client / AsyncDatabase accessor
    models.py       NisseDbModel for Mongo docs: `_id`↔`id` + audit fields (created_at/updated_at/deleted_at, soft-delete)
    providers.py    role → model_id presets (main · judge · curator · task)

  chat/             Telegram I/O — the ONLY aiogram Router
    router.py       voice + text handler → transcribe → Assistant.reply() → answer
    transcribe.py   voice file → text (STT adapter; provider-swappable)

  assistant/        the main agent — composition root
    assistant.py    Assistant.reply(conversation_id, text) -> str; thin TG↔agent layer over the registry
    conversations.py Conversations — registry: builds each chat's agent once and caches it
    conversation.py Conversation — one chat's reused agent + history + scratchpad; runs one reply (lock-serialized)
    history.py      MongoMessageHistory — per-conversation transcript persisted to Mongo (`conversations`)
    prompt.py       base system prompt (effective = base + curator overlay from Mongo)
    toolset.py      assembles tools: always-on core + code skills + learned skills

  judge/            provider-agnostic LLM-as-judge (Gemini grades Opus output)
    judge.py        Judge interface + Verdict model (score / pass / rationale)
    gemini.py       Gemini/Vertex impl — own client, own provider
    rubric.py       scoring criteria

  memory/           long-term memory: durable owner-facts + the remember/read/forget tools
    store.py        Memory model + MemoryStore (Mongo `memories`, short-id CRUD)
    tools.py        remember · read_memory · forget; index injected via the read tool's user_message()
    recall.py       (future) Active Memory — bounded pre-reply recall over LongTerm

  scheduling/       self-invocation: one-off reminders + recurring routines (webhook mode only)
    store.py        ScheduledTask + ScheduleStore (scoped, for tools) + claim/reschedule/mark_done (runner, by id)
    tools.py        remind · schedule_routine · cancel_schedule (injects active-schedule list); agent gives UTC, asks owner's TZ
    dispatch.py     Scheduling holder + enqueue_fire (reuses baski CloudTasksScheduler)
    runner.py       ScheduleRunner.fire — CAS-claim → re-arm if recurring → Assistant.reply → send
    router.py       POST /schedule/fire — Cloud Tasks worker (mounted via add_webhook_routes)

  search/           SerpApi search tools — 10 leaf tools over baski's SerpApiClient
    serp_tool.py    SerpTool base (params→request→render) + shared format_hits (token-lean Markdown)
    tools.py        google_search · google_ai_mode · google_maps_search · google_news · google_events ·
                    amazon_search→amazon_product · youtube_search→youtube_transcript · google_jobs
                    (discovery→detail chains share an entity id; design: docs/serpapi-search-tools.md)

  curator/          nightly self-maintenance agent (off the request path)
    curator.py      scans the day's chats → maintain knowledge, learn skills, tune prompt
    router.py       HTTP trigger Cloud Scheduler hits nightly (/curate)

  tools/            (future) more nisse-specific leaf Tool classes — one per file, thin wrapper over one API
    google/ (gmail·calendar·tasks·drive) / perplexity.py … — created when needed
    Each is WIRED in `Conversations._build_<domain>_tools()` (no provider registry). Search lives in
    `search/` (nisse SerpApi leaves); browse uses baski's WebBrowseTool — both wired in `_build_web_tools()`.
    (+ external MCP servers as an optional secondary tool source — hybrid)

  skills/           code skills — dev-authored bundles (Python, may wrap a sub-agent)
    research/       research SUB-AGENT (own Agent + search tools)
      agent.py      the sub-agent loop
      skill.py      delegate-to-research Tool exposed to the main agent
                    (learned skills are data specs in Mongo, not code here)
```

`hello/` is the throwaway placeholder router — replace it with `chat/`.

## Module shape

Same self-contained-module discipline as clarity, adapted: a module exports
exactly **one** name from its `__init__.py` — `router` (Telegram/HTTP),
`Assistant`, `tools`, or `skill`.

- **Tool** = subclass `baski.agents.Tool`; declare `name / one_line /
  description / input_schema`; implement `async execute(**kwargs) -> str`.
  One-shot, returns a string. Most tools are stateless; a tool that **persists state
  across replies** (anything backed by Mongo — memory today) is the exception and MUST be
  **scoped to the `conversation_id`** so one chat can never read or write another's data.
  baski is conversation-agnostic (generic framework, no persistence) — the scope is bound
  *here*, by `Conversations._build_<domain>_tools(conversation_id)`, which constructs the
  per-conversation store and passes it to the stateful tools (see "Dependency wiring" below).
  **A tool talks only to a narrow domain SERVICE, never to raw clients/transport.** Inject a
  service that hides the wiring behind intent-named methods — e.g. scheduling tools get a
  `SchedulingService.enqueue_fire(...)`, not the Cloud Tasks scheduler + callback URL. Passing a
  client bundle (scheduler+endpoint) into a tool leaks the transport into the tool and couples it
  to how the work is dispatched; the service is the seam that keeps the tool ignorant of that.
- **Skill** = a tool bundle (and optionally a sub-agent) the toolset can load.
  Two kinds: **code skills** = a new `skills/<x>/` + one line in `skills/__init__.py`
  (dev-authored, git-versioned); **learned skills** = data specs (name + prompt +
  refs to existing tools) the curator writes to Mongo and the toolset loads at runtime.
- **Sub-agent** = its own `baski.agents.Agent` with a narrow toolset, wrapped by
  a single delegating Tool. Use a sub-agent for multi-step / stateful work
  (research, browsing). Use a plain tool for a single API call. Don't make an
  agent out of something a pure function can do.
- **Handlers are thin** — Telegram/HTTP handlers are 2–5 line delegations into
  `Assistant` / a service. No business logic, no external IO inline in a handler.
- **`shared/` only when reused** — promote code here once a *second* domain needs
  it; don't pre-share. It holds primitives (db, base models, providers), not logic.
- Pydantic in / Pydantic out between functions; raw dicts only for pipeline
  intermediates and logger labels. Keep `reply` / `execute` short orchestrators.
- **Every class docstring states its lifecycle** — *long-lived* (singleton / per-conversation,
  built once and reused) vs *short-lived* (per-reply glue, or a per-record data model) — so it's
  clear at a glance what's cached and what's rebuilt each time.

## Direction: a self-extensible agent

A standing goal that shapes the design: the agent grows its own capabilities at
runtime, without a code deploy. Two independent vectors of extension:

1. **Self-authored skills** — the agent learns *how to do a task* as a reusable
   sequence of steps, then saves it as a skill it can replay later. E.g. "book a
   restaurant" decomposes into: find restaurants in radius with good metrics →
   ask the user for price range → book via the Google Maps link. The agent
   captures that recipe once and reuses it. (This is the learned-skills mechanism
   below, raised to a first-class design intent.)

2. **Self-loading tools** — the tool catalog is extensible by data, not only by
   code. Tools come in several *types*; the first type is an **HTTP-service tool**:
   a config record in the DB describing a base URL, one of several auth methods,
   and a set of HTTP endpoints (each endpoint = one callable action). A client
   ingests an API's docs, writes the resulting schema row to the DB, and the tool
   is picked up automatically — no Python, no redeploy.

Both vectors share the same principle already in this doc: runtime-editable
capability lives in **Mongo, never in code**. Rationale and design detail belong
in `IDEAS.md`.

## Tool tiers (always-on vs loaded) — built in from the start

- **Always-on** (assembled in `Conversations._build`, present every turn):
  knowledge/memory, context management (`delete_messages`), `remind`/`schedule_routine`/`cancel_schedule` (webhook mode).
- **Loaded**: a skill's tools, exposed only when that skill is active. The
  toolset selects which skills to expose per request and injects only their
  schemas — the model never sees the full catalog at once.

## Dependency wiring — assemble tools inline in `Conversations._build`

`CoreDeps` (`shared/deps.py`) holds the low-level clients that carry **network + auth**, built
once in `backend.py`: logger, http, anthropic, database, playwright, bucket_name, and the Cloud
Tasks `scheduler` + `schedule_endpoint` (both `None` in polling). Everything else — per-domain
mid-level clients, conversation-scoped stores, services, and the tools — is assembled **on the
stack** in `Conversations._build`, from `CoreDeps` alone.

**The pattern: one `_build_<domain>_tools()` per domain.** Each takes what it needs (`self._deps`,
`conversation_id`) and returns `list[Tool]`; `_build` adds them all. To add a tool domain, write a
new `_build_<domain>_tools()` and call it — nothing else changes.

```python
def _build_memory_tools(self, conversation_id):      # store scoped to the chat
    store = MemoryStore(self._deps.database, conversation_id=conversation_id)
    return [RememberTool(store), RecallMemoryTool(store), ForgetTool(store)]

def _build_scheduling_tools(self, conversation_id):  # webhook mode only — [] when no scheduler
    scheduler, endpoint = self._deps.scheduler, self._deps.schedule_endpoint
    if scheduler is None or endpoint is None:
        return []
    service = SchedulingService(scheduler=scheduler, endpoint=endpoint)
    store = ScheduleStore(self._deps.database, conversation_id=conversation_id)
    return [RemindTool(store, service), RoutineTool(store, service), CancelScheduleTool(store)]
```

No provider registry, no `build_tools` flatten — that indirection was ceremony. A network client
that needs config/auth (Cloud Tasks) goes in `CoreDeps`; the cheap per-conversation assembly lives
in `_build`. Bimodality (cloud has Cloud Tasks, polling doesn't) is one honest `None` field in
`CoreDeps`, not an optional threaded through `Assistant`/`Conversations`.

## Memory — three tiers (≠ conversation transcript)

`MessageHistory` (baski.agents) is the transcript / context window, not memory. In chat
mode it's `MongoMessageHistory` (`assistant/history.py`), persisted per conversation.

- **ShortTerm** — per-Turn scratchpad (baski `ShortTermMemory`, `store_memory` tool);
  lives during one `Assistant.reply()`, wiped after the reply. Not persisted.
- **LongTerm** — durable owner-facts in Mongo (`memory/`), shipped now: a flat store
  + three tools (`remember` / `read_memory` / `forget`). **Scoped per `conversation_id`** —
  `MemoryStore(database, conversation_id=…)` filters every read/write to one chat, so
  memories never cross conversations. The titled index is always
  injected (read tool's `user_message()`); bodies fetched on demand by `public_id`.
  Each memory carries `category ∈ {fact,preference,event}`, `source ∈ {user,external,agent}`,
  body, audit timestamps. **Two ids:** durable DB `id` (Mongo ObjectId) vs short agent-facing
  `public_id` (what the model reads/echoes). `forget` is a **soft delete** — it sets
  `deleted_at`; reads filter `{"deleted_at": None}`. The agent forgets a memory itself when
  the dialogue shows it's stale.

Every tool injects its always-present block via `Tool.user_message()` (baski) — the
uniform seam ShortTerm, the memory index, and future skills all share.

Future: a curated always-hot subset + Active Memory recall (`memory/recall.py`) + the
curator promoting/expiring facts. Rationale and prior art: `IDEAS.md`.

## Manual probe (end-to-end testing — any feature)

`app/probe.py` runs the agent once, outside Telegram, and prints the three things to check
on any feature — read from the agent's own trace:

1. **Injected context** — the system prompt + the first-turn messages the agent received.
   Confirm what reaches the model is what you expect.
2. **Tool calls** — every tool invoked, with arguments. Confirm the agent calls the expected ones.
3. **Answer** — the final reply is sensible.

```
make probe MSG="…" [U=<id>]      # one agent run; prints injected context, tool calls, answer
make memories                    # dump the `memories` collection (live + soft-deleted)
```

Where to look, per run:
- **Injected context** is the ground truth for what the model saw — read it first.
- **`U=` is the conversation id.** Testing recall/contradiction? Use a *fresh* `U=`: in the same
  conversation the fact is still in the transcript, so the agent answers from there and the
  long-term path never runs. A fresh `U=` has an empty transcript but the same global memory store.
- The probe shows what the agent *did*; `make memories` shows the durable *result* in Mongo.

Needs the same env as `make backend-run` (loaded from `.env`); it makes real API/DB calls and
writes to the real DB — use a throwaway `U=`. Write expectations **before** running. The memory
cases (scripted + natural-conversation, expectation-first) live in `docs/memory-test-cases.md`;
future features get their own cases doc on the same pattern.

## Scheduling (self-invocation) — webhook mode only

Three tools: `remind` (one-off), `schedule_routine` (recurring every N hours), and `cancel_schedule` (cancels by id; its `user_message` injects the active-schedule list each turn). The two creation tools store a
`ScheduledTask` (conversation-scoped) and enqueue ONE Cloud Task per occurrence with `schedule_time`
(push, no poller) — reusing baski's `CloudTasksScheduler`, no Cloud Scheduler resource.

- **Time is the agent's job.** The agent gives an absolute UTC `fire_at`; if it doesn't know the
  owner's city/timezone it asks (and remembers it) — nisse invents no timezone. `fire_at` is
  normalized to UTC seconds so it round-trips Mongo's ms datetimes and the claim matches exactly.
- **Fire** (`POST /schedule/fire`, mounted via `add_webhook_routes`): `ScheduleRunner.fire` →
  **atomic CAS-claim** (`claim`: PENDING→RUNNING for this public_id+fire_at — the single idempotency
  point under Cloud Tasks' at-least-once delivery) → recurring: advance + re-enqueue the next
  occurrence BEFORE running → `Assistant.reply(conversation_id)` (same agent as a live turn) →
  `bot.send_message` → ONCE: mark DONE. A duplicate delivery loses the claim and no-ops.
- **No app-level OIDC check** on the route — same protection as baski's `/tasks/update` worker
  (Cloud Tasks OIDC + Cloud Run ingress). Polling mode wires no scheduling tools (no public callback).

## Judge / evaluation (Gemini grades Opus)

One provider-agnostic `Judge` in `app/judge/` (Gemini as first impl, with its own
client — the main agent loop stays Anthropic-only). Two consumers share it:

- **Inline gate** — `Assistant.reply()` drafts an answer, the judge scores it
  against the rubric; below threshold → one refine pass, then send. Off per turn
  by default (config flag) — it doubles cost/latency.
- **Offline harness** — top-level `evals/` (sibling to `tests/`, outside `app/`):
  a scenario dataset + a runner that replays `Assistant.reply()` and scores each
  with the judge. Run in CI / locally as skills grow, to catch regressions.

Judge input is what `TraceCollector` records (input, final answer, tool calls);
inline it scores the draft before the trace finalizes.

## Self-maintenance (nightly curator)

One background agent, **nightly only** (Cloud Scheduler → `curator/router.py`
`/curate`); no per-turn reflection. It reviews the day's chats (may use `judge`)
and does three jobs:

- **Memory** — extract durable facts into long-term, refresh, expire stale ones.
- **Learn skills** — when a repeated pattern emerges, write a learned-skill spec.
- **Tune prompt** — edit the system-prompt overlay.

It **auto-applies** — every mutation is an append-only versioned record in Mongo,
so any change is revertible (rollback). Consequence baked into the layout above:
runtime-editable state lives in **Mongo, never in code** — the curator never
writes Python to the container. Effective prompt = base `prompt.py` + versioned
overlay; learned skills = specs loaded by `toolset.py` alongside `skills/`.

## baski building blocks (don't reinvent)

`telegram.server.TelegramServer` · `telegram.receptionist.Receptionist` ·
`telegram.history.ChatHistory` · `telegram.storage.UsersStorage` ·
`server.AppConfig` / `Logger` · `agents.{Agent,Tool,ToolSet,MessageHistory,
ShortTermMemory,TraceCollector}` · `clients.{SerpAPIClient,PlaywrightClient}` ·
`primitives.{datetime,json,unique_id}` · `pattern.retry` · `map_async`.
