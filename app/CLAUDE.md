# nisse backend — agent-centric Telegram assistant

One LLM agent loop is the core. Telegram is a thin I/O skin: a voice/text
message comes in, gets transcribed, goes into `Assistant.reply()`, the agent
decides which tools/sub-agents to call, the reply goes back out. Everything
else (memory, scheduling, search, Google Suite) is a **tool** the agent calls.

Built on `baski`. The agent framework (`Agent` / `Tool` / `ToolSet` /
`MessageHistory` / `ShortTermMemory` / `TraceCollector`) is ported from
clarity-auto-care into `baski.agents` — import from there, do not vendor.

**`Agent` (baski) is abstract; `Assistant` (this app) is concrete.** `baski.agents.Agent` is a
transport-agnostic LLM loop (tools, messages, tracing) — it knows nothing about Telegram, this
user, or this product. `Assistant` holds all that: the Telegram channel, the owner, the wired
tools/skills, the system prompt. Keep the line clean — no Telegram type, chat id, or aiogram import
reaches `baski`, and `Assistant` never reimplements the loop. Generic → `baski.agents`;
"because we're a Telegram assistant for this user" → `Assistant`.

Concepts, rationale, and source pointers live in `IDEAS.md` — this file is project structure only.

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
    router.py       voice + text handler → transcribe → Assistant.reply(on_event=TelegramProgress) → answer
    progress.py     TelegramProgress — baski AgentEvents → ONE live-edited message: step checklist
                    (each tool with its salient arg in a `code span`, thinking — a rotating "думаю…"
                    word when thinking surfaces no text), then the reply streamed in a sentence at a
                    time (on baski's TextDelta), throttled to 0.5s; finish() renders the final MarkdownV2.
    format.py       LLM markdown → Telegram MarkdownV2 via telegramify-markdown; size-split; plain fallback
    transcribe.py   voice file → text (STT adapter; provider-swappable)

  assistant/        the main agent — composition root
    assistant.py    Assistant.reply(conversation_id, text) -> str; thin TG↔agent layer over the registry
    conversations.py Conversations — registry: builds each chat's agent once and caches it
    conversation.py Conversation — one chat's reused agent + history + scratchpad; runs one reply (lock-serialized)
    history.py      MongoMessageHistory — transcript in Mongo `conversation_turns`, one doc per turn (soft-deleted when pruned); turns are `MongoTurn` (baski `Turn` + `created_at`); format_for_api strips thinking blocks from settled turns (Opus 4.5+ bills replayed thinking), marks the prompt-cache breakpoint on the last turn (baski `mark_cached`), and stamps each `[Turn N]` marker with the turn's absolute UTC send-time on the first turn and after a >1h gap (`_turn_marker`) so the model can judge recency — absolute (never relative) to stay byte-stable in the cached prefix, normalized via baski `as_utc`; the volatile `[Context: N% used]` footer rides after the breakpoint via `context_status()`; truncate sizes the window via baski `effective_input_tokens` (incl. cached prefix), not raw `input_tokens` — prompt caching shrinks the latter
    prompt.py       base system prompt (effective = base + curator overlay from Mongo)
    toolset.py      assembles tools: always-on core + code skills + learned skills

  judge/            provider-agnostic LLM-as-judge (Gemini grades Opus output)
    judge.py        Judge interface + Verdict model (score / pass / rationale)
    gemini.py       Gemini/Vertex impl — own client, own provider
    rubric.py       scoring criteria

  memory/           LONG-TERM MEMORY: durable owner-facts + the recall_save/recall_read/recall_edit/recall_forget tools
    store.py        Memory model + MemoryStore (Mongo `memories`, short-id CRUD: add/overwrite/set_body/soft_delete)
    tools.py        recall_save · recall_read · recall_edit · recall_forget; index injected via the read tool's user_message()
    recall.py       (future) Active Memory — bounded pre-reply recall over LongTerm

  lists/            ARTIFACT TIER: named mutable lists (shopping/todo/watchlist) — NOT memory
    store.py        ItemList + ListStore (Mongo `lists`, one doc per (conversation_id, name); case-folded name, deduped items, in-place edit, soft-delete)
    tools.py        list_edit (add/remove/clear in one call) · list_show; list-name index injected via list_show's user_message()

  prompts/          living system-prompt fragments the bot maintains, per conversation, by type
    store.py        Prompt + PromptType(StrEnum) + PromptStore (Mongo `prompts`, one doc per (conversation_id, prompt_type), overwritten in place)
    tools.py        update_core_memory — the always-on CORE MEMORY block (behaviour rules + owner identity + current focus); injected into the system EVERY turn via the tool's async system_prompt(); edited like a list (add/remove whole lines in one call, mirrors list_edit + shared match_unique) so the agent touches only named lines and never rewrites the block wholesale, size-capped so it stays lean

  scheduling/       self-invocation: one-off reminders + recurring routines (webhook mode only)
    store.py        ScheduledTask + ScheduleStore (scoped, for tools) + claim/reschedule/mark_done (runner, by id)
    tools.py        remind · schedule_routine · cancel_schedule (injects active-schedule list); agent gives UTC, asks owner's TZ
    service.py      SchedulingService.enqueue_fire (reuses baski CloudTasksScheduler) + LoggingScheduler stand-in
    runner.py       ScheduleRunner.fire — CAS-claim → re-arm if recurring → Assistant.reply → send
    router.py       POST /schedule/fire — Cloud Tasks worker (mounted via add_webhook_routes)

  search/           SerpApi search tools — 10 leaf tools over baski's SerpApiClient
    serp_tool.py    SerpTool base (params→request→render) + shared format_hits (token-lean Markdown)
    tools.py        google_search · google_ai_answer · google_maps_search · google_news · google_events ·
                    amazon_search→amazon_product · youtube_search→youtube_transcript · google_jobs
                    (discovery→detail chains share an entity id; design: docs/serpapi-search-tools.md)

  browser/          logged-in browser ACTIONS (distinct from read-only browse_website) — design: docs/browser-actions.md
    session.py      BrowserSession — one isolated Chromium context per chat (own cookie jar), read as an accessibility tree (aria_snapshot, no screenshots), acted on by role+name; lazy context, optional per-host proxy
    tools.py        web_open · web_snapshot · web_click · web_type — each returns the post-action a11y tree; wired in Conversations._build_browser_action_tools(); session loaded from browser_state_path(chat) captured by `make startbrowser`
    proxy.py        ProxyPool — sticky one-proxy-per-host, rotate on mark_banned; loaded from BROWSER_PROXIES (Webshare host:port:user:pass lines; token stays out of the app)

  curator/          nightly self-maintenance agent (off the request path)
    curator.py      scans the day's chats → maintain knowledge, learn skills, tune prompt
    router.py       HTTP trigger Cloud Scheduler hits nightly (/curate)

  tools/            (future) more nisse-specific leaf Tool classes — one per file, thin wrapper over one API
    google/ (gmail·calendar·tasks·drive) / perplexity.py … — created when needed
    Each is WIRED in `Conversations._build_<domain>_tools()` (no provider registry). Search lives in
    `search/` (nisse SerpApi leaves); read-only browse uses baski's WebBrowseTool — both wired in `_build_web_tools()`;
    logged-in browser actions live in `browser/`, wired in `_build_browser_action_tools()`.
    (+ external MCP servers as an optional secondary tool source — hybrid)

  skills/           code skills — dev-authored bundles (Python, may wrap a sub-agent)
    research/       research SUB-AGENT (own Agent + search tools)
      agent.py      the sub-agent loop
      skill.py      delegate-to-research Tool exposed to the main agent
                    (learned skills are data specs in Mongo, not code here)
```

`judge/`, `curator/`, `skills/`, `tools/` are design intent (not built yet); the sections below
describe them. Shipped today: `chat`, `assistant`, `memory`, `prompts`, `scheduling`, `search`, `shared`.

`Tool.system_prompt()` (baski) is **async and re-read every turn** (symmetric with `user_message()`);
the agent reassembles its system prompt each turn, so a tool can inject live content — `prompts/`
uses this to keep the owner-preference block always current.

## Module shape

Same self-contained-module discipline as clarity, adapted: a module exports
exactly **one** name from its `__init__.py` — `router` (Telegram/HTTP),
`Assistant`, `tools`, or `skill`.

- **Tool** = subclass `baski.agents.Tool`; declare `name / one_line / description /
  input_schema`; `async execute(**kwargs) -> str`. One-shot. Stateless by default; a tool that
  **persists across replies** (Mongo-backed, e.g. memory) MUST be **scoped to `conversation_id`**
  so chats never cross. baski is persistence-agnostic — the scope is bound *here* in
  `Conversations._build_<domain>_tools(conversation_id)` (see Dependency wiring). **A tool talks to
  a narrow domain SERVICE, never raw clients/transport** — scheduling tools get
  `SchedulingService.enqueue_fire(...)`, not the Cloud Tasks scheduler + URL. The service is the
  seam that keeps the tool ignorant of how work is dispatched.
  - **`execute` must return a clear, actionable result.** On success, state plainly WHAT was done (the
    new state). On failure or partial success, say WHAT went wrong and how to fix it so the model can
    self-correct on the next turn — e.g. "ambiguous, several match — be more specific", not a bare
    "failed" or a silent no-op. The result string is the model's only feedback channel.
  - **Keep `description` / `system_prompt` / field descriptions CONCISE.** They ride the context
    window on EVERY turn (measured tax — see `docs/context-pruning.md`). Every word must earn its slot;
    cut examples and restatement, keep the load-bearing rule.
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

Standing goal: the agent grows capabilities at runtime, no deploy. Two vectors:

1. **Self-authored skills** — the agent captures *how to do a task* as a reusable step sequence
   and replays it later (e.g. "book a restaurant" = find in radius with good metrics → ask price
   range → book via the Maps link). The learned-skills mechanism below, as first-class intent.
2. **Self-loading tools** — the catalog extends by data, not code. First type: an **HTTP-service
   tool** — a DB record (base URL, auth method, endpoints = callable actions). A client ingests an
   API's docs, writes the schema row, the tool appears automatically — no Python, no redeploy.

Both: runtime-editable capability lives in **Mongo, never in code**. Detail in `IDEAS.md`.

## Tool tiers (always-on vs loaded) — built in from the start

- **Always-on** (assembled in `Conversations._build`, present every turn):
  knowledge/memory, context management (`prune_transcript`), `remind`/`schedule_routine`/`cancel_schedule`.
- **Loaded**: a skill's tools, exposed only when that skill is active. The
  toolset selects which skills to expose per request and injects only their
  schemas — the model never sees the full catalog at once.

## Dependency wiring — assemble tools inline in `Conversations._build`

`CoreDeps` (`shared/deps.py`) holds the network+auth clients built once in `backend.py` (logger,
http, anthropic, database, playwright, bucket, scheduler, schedule_endpoint). Everything else —
per-domain stores, services, tools — is assembled on the stack in `Conversations._build` from
`CoreDeps` alone.

**The pattern: one `_build_<domain>_tools(conversation_id)` per domain**, returning `list[Tool]`.
To add a tool domain, write one and call it — nothing else changes. A stateful tool gets its
conversation-scoped store built here; no provider registry, no flatten indirection. The scheduler
is always present (a `LoggingScheduler` stand-in in polling/probe), so scheduling tools exist in
every mode — only webhook mode actually fires the callback.

## Memory — three tiers (≠ conversation transcript)

`MessageHistory` (baski.agents) is the transcript / context window, not memory. In chat
mode it's `MongoMessageHistory` (`assistant/history.py`), persisted per conversation.

- **ShortTerm** — per-Turn scratchpad (baski `ShortTermMemory`, `working_note` tool);
  lives during one `Assistant.reply()`, wiped after the reply. Not persisted.
- **LongTerm** — durable owner-facts in Mongo (`memory/`), shipped now: a flat store
  + four tools (`recall_save` / `recall_read` / `recall_edit` / `recall_forget`). **Scoped per `conversation_id`** —
  `MemoryStore(database, conversation_id=…)` filters every read/write to one chat, so
  memories never cross conversations. The titled index is always injected (read tool's
  `user_message()`) — one pointer per memory carrying source *kind* + `updated_at` (the freshness
  date, so an edited memory doesn't look stale); the body and the external `source.ref`/url are
  fetched on demand by `public_id` (kept out of the every-turn index to save tokens).
  Behaviour rules, owner identity that shapes most turns, and current focus do NOT go here — they live
  in core memory (`prompts/`, `update_core_memory`), always-on in the system prompt. LongTerm is the
  recalled-on-demand long tail: discrete `fact`/`event` only (the `preference` category was removed).
  See `app/memory/CLAUDE.md` for the core-vs-recall routing rule and why.
  Each memory carries `category ∈ {fact,preference,event}`, `source ∈ {user,external,agent}`,
  body, audit timestamps. **Two ids:** durable DB `id` (Mongo ObjectId) vs short agent-facing
  `public_id` (what the model reads/echoes). **Correcting a memory is in place** (not delete-then-recreate,
  which churns the id and corrupts long bodies): `recall_save(public_id, …)` overwrites the whole record
  (`MemoryStore.overwrite` — upserts to a fresh id if the old one is gone, since the unique `public_id`
  index forbids reuse); `recall_edit(public_id, old, new)` patches the body (`MemoryStore.set_body`) —
  replace `old` with `new`, or append when `old` is empty, with the always-accepted create/append paths
  chosen so the model's first tool call rarely gets rejected. `recall_forget` is a **soft delete** — it sets
  `deleted_at`; reads filter `{"deleted_at": None}` — only for facts that no longer hold. The agent
  corrects or forgets a memory itself when the dialogue shows it's stale.

Every tool injects its always-present block via `Tool.user_message()` (baski) — the
uniform seam ShortTerm, the memory index, and future skills all share.

Future: a curated always-hot subset + Active Memory recall (`memory/recall.py`) + the
curator promoting/expiring facts. Rationale and prior art: `IDEAS.md`.

## ARTIFACT tier — lists (`lists/`), NOT a memory tier

A mutable named collection the owner edits over time (shopping, todo, watchlist) is an **artifact**,
not memory. It lives in `lists/` (Mongo `lists`, scoped per `conversation_id`), with `list_edit`
(add/remove/clear) + `list_show` — two tools, kept minimal because every tool's schema rides the
context window on every turn (4 verbs → 2 saved ~268 tok/turn, measured). **Do not put lists in long-term memory** — a list
isn't a discrete `fact`/`event`, and storing one in `memories` produced duplicated, contradicting
copies (the bug that motivated this tier). The routing the agent follows: *something you add to and
cross off* → a list; *an inert fact recalled by topic* → memory; *a behaviour/identity rule* → core.
Reminders/routines are already their own artifact store (`scheduling/`, `ScheduledTask`). See
`app/lists/CLAUDE.md`.

## Manual probe (end-to-end testing — any feature)

`app/probe.py` runs the agent once, outside Telegram, and prints the three things to check
on any feature — read from the agent's own trace:

1. **Injected context** — the system prompt + the first-turn messages the agent received.
   Confirm what reaches the model is what you expect.
2. **Tool calls** — every tool invoked, with arguments. Confirm the agent calls the expected ones.
3. **Answer** — the final reply is sensible.

```
make probe MSG="…" [U=<id>]      # one agent run; prints injected context, tool calls, answer
make memories                    # dump `memories` (live + soft-deleted)
make turns U=<id>                # dump one conversation's `conversation_turns` (active + soft-deleted)
```

- **Injected context** is the ground truth for what the model saw — read it first.
- **`U=` is the conversation id** (an int). Testing recall/contradiction? Use a *fresh* `U=`: in the
  same conversation the fact is still in the transcript, so the long-term path never runs.
- The probe shows what the agent *did*; `make memories`/`make turns` show the durable *result* in Mongo.

Real API/DB calls (env from `.env`) — use a throwaway `U=`. Write expectations **before** running.

### Prod traces (real traffic)

The probe writes its trace to a temp dir (gone after the run). **Prod** persists every agent run two ways:
the full trace (gzipped JSON) to GCS, and a lightweight summary to the Mongo `traces` collection. To
inspect what the live bot actually did, pull the latest from GCS:

```
# bucket nisse2050-private, prefix traces/, one file per run: <trace_id>.json.gz
gcloud storage ls --long gs://nisse2050-private/traces/ | sort -k2 | tail   # newest trace_ids by time
gcloud storage cat gs://nisse2050-private/traces/<trace_id>.json.gz | gunzip | jq   # full trace
```

The Mongo `traces` summary (`_id`=trace_id, `created_at`, `user_request`[:128], model, tokens, cost,
`error`) is the index — query it by `created_at` desc to pick a `trace_id`, then fetch the body from GCS.
Needs read access to GCP project `nisse2050` (the bucket name is global, but auth isn't).

**Testing is part of every task — the definition of done.** Each feature has an expectation-first
cases doc (`docs/memory-test-cases.md`, `docs/history-test-cases.md`). A task isn't done until you
have: added scenarios covering the new behavior, run them, AND re-run the related existing scenarios
to confirm no regression. New feature → new cases doc on the same pattern.

## Scheduling (self-invocation) — fires in webhook mode

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
  (Cloud Tasks OIDC + Cloud Run ingress). The tools exist in every mode; only webhook mode has the
  public `/schedule/fire` callback, so only there does a fire actually run.

## Judge / evaluation (Gemini grades Opus) — planned

Provider-agnostic `Judge` in `app/judge/` (Gemini first, own client — the main loop stays
Anthropic-only). Two consumers: an **inline gate** (`Assistant.reply()` scores its draft against a
rubric, one refine pass below threshold; off by default — doubles cost/latency) and an **offline
harness** (`evals/`, sibling to `tests/`: a scenario dataset replayed through `Assistant.reply()`
and scored to catch regressions). Judge input = what `TraceCollector` records.

## Self-maintenance (nightly curator) — planned

One background agent, nightly only (Cloud Scheduler → `/curate`); no per-turn reflection. It reviews
the day's chats and does three jobs: extract/refresh/expire **memory**, write **learned-skill** specs
for repeated patterns, and **tune the prompt** overlay. Every mutation is an append-only versioned
Mongo record (revertible) — runtime-editable state lives in **Mongo, never in code**; effective
prompt = base `prompt.py` + overlay, learned skills = specs loaded alongside `skills/`.

## baski building blocks (don't reinvent)

`telegram.server.TelegramServer` · `telegram.receptionist.Receptionist` ·
`telegram.history.ChatHistory` · `telegram.storage.UsersStorage` ·
`server.AppConfig` / `Logger` · `agents.{Agent,Tool,ToolSet,MessageHistory,
ShortTermMemory,TraceCollector}` · `clients.{SerpAPIClient,PlaywrightClient}` ·
`primitives.{datetime,json,unique_id}` · `pattern.retry` · `map_async`.
