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
    deps.py         CoreDeps — the shared clients + services every tool is built from
    models.py       NisseDbModel for Mongo docs: `_id`↔`id` + audit fields (created_at/updated_at/deleted_at, soft-delete);
                    PublicIdModel adds the short agent-facing `public_id` (memories, scheduled tasks)
    revisions.py    CHANGE HISTORY: append-only `revisions` (collection/target/kind/before/after/
                    actor/run_id). Every content-LOSING write records the text it destroyed, so an
                    unattended curator edit is readable and undoable. Who is writing is ambient —
                    `acting_as(Actor.CURATOR, run_id=…)` wraps the nightly pass; outside it the
                    actor is the assistant. A separate collection, NOT a second version in place:
                    `memories`/`lists` unique indexes deliberately span soft-deleted docs (list
                    revive, public_id never reused), so a superseded copy beside the live one
                    would collide

  chat/             Telegram I/O — the ONLY aiogram Router
    router.py       `ChatRouter` (holds assistant + transcriber + speaker + the `questions` registry;
                    `.build(saved=, reactions=)` assembles the aiogram Router). Text/voice/audio/photo/PDF handler → transcribe voice+audio-file / attach photo+PDF (Media) →
                    Assistant.reply(on_event=TelegramProgress) → answer. Photos → JPEG image block;
                    documents → image or application/pdf block if the model reads it (else declined); >20MB declined
                    (voice in → also voices the reply back via Speaker; best-effort, never blocks the text answer).
                    Also registers the ask.py callback_query handler (button taps aren't messages)
                    and the saved.py viewer commands — both BEFORE the catch-all, which would
                    otherwise swallow a command into an agent turn (aiogram tries handlers in order).
    saved.py        READ-ONLY VIEWER over what the agent saved: `/lists` · `/memory` · `/core` ·
                    `/schedules` · `/help`. Owns the command names (`SavedCommand`), the menu it
                    publishes itself on router startup (`set_my_commands`, retried then degraded to a
                    warning — a cosmetic menu must never abort boot), and the handlers — so the
                    published menu and the handled commands can't drift. Reads the four stores
                    directly: no model call, no tokens, verbatim content (the agent's own summary is
                    what the owner couldn't audit). Per Telegram's guidance: one specific command per
                    store rather than `/show <what>`, and drill-down EDITS the message in place
                    instead of sending a new one. The two unbounded stores (lists, memories) render as
                    a tapable index — 8 entries/page, tap opens the entry + ⬅️ Назад, ‹ › page — while
                    core memory, the schedule list and `/help` just print. Buttons carry the entry's
                    POSITION (a name/title would blow the 64-byte callback payload); the store is
                    re-read on every tap, so a position that no longer exists falls back to the fresh
                    index. An opened entry stays in ONE message (so ⬅️ Назад can restore the index by
                    editing it) — an over-long record is cut with a note saying so, never spilled into
                    extra messages that would orphan below the restored index. Plain text, no
                    MarkdownV2 — the content is the owner's own words, shown byte-for-byte.
                    `/help` closes with what the owner can't discover by reading commands: that
                    reactions are the cheap feedback channel, and that the nightly curator edits
                    these same stores and reports in the morning.
    reactions.py    ReactionRecorder — the `message_reaction` update → one append-only Mongo record.
                    Registering the handler IS the wiring, and it gates on the allow-list by hand
                    (`AllowlistMiddleware` sits on `message` only); rationale in the module docstring.
                    Telegram names only a message, so it resolves the turn behind it through
                    `TurnLookup` (injected by the wiring — a runtime import would cycle) and stores
                    the `turn_id` on the record.
    ask.py          the ask_user TOOL: mid-turn clarifying question with tappable options. Agent calls it like
                    any tool; the owner sees an inline keyboard; the call BLOCKS on an in-memory asyncio.Future
                    until they tap, then returns the choice (single=one tap; multi=toggle+Done; plus "None of
                    these"). Every parked question lives in one `PendingQuestions` registry, held on
                    `CoreDeps.questions` and handed to both the tool and the router —
                    one registry the tool, the tap handler, a typed reply and the probe all go through.
                    One process/event loop (max_instances=1) so the tap resolves the parked turn's
                    Future in memory — no queue. The callback handler resolves it DIRECTLY, never via
                    Assistant.reply (whose per-chat lock the parked turn holds). Needs the Bot → CoreDeps.bot
                    (required). At most ONE open question per chat — a second one is refused, since a typed
                    answer is routed by chat alone. Timeout is a module constant (300s).
                    Whether the agent CHOOSES to ask is the only thing worth measuring here, so the probe
                    supplies a fake bot and taps through `questions.resolve_tap` — see Manual probe, cases in
                    `docs/ask-test-cases.md`. A TYPED answer counts too: the router calls its registry's `answer`
                    before starting a turn, since the parked turn holds the chat lock the new message
                    would queue behind.
    progress.py     TelegramProgress — baski AgentEvents → ONE live-edited message, rendered as an
                    ordered list of segments (`_Seg`: process | text | judge) so tools, model text,
                    and judge verdicts stay interleaved in the exact order they happened — nothing
                    dropped. A process block (tools + thinking) renders as a `>` blockquote (each tool
                    a human label icon+verb via `_TOOL_LABELS`, salient arg in a `code span`; thinking
                    — a rotating "думаю…" word when it surfaces no text); model text (Message + the
                    current turn streamed a sentence at a time via TextDelta, throttled to 0.5s)
                    renders plain; a `Judged` verdict renders bold (`**⚖️ …**`) right after the text
                    it graded (that draft is kept, never wiped). finish(result) settles the whole
                    stream + cost footer; finish_text(text) is the error/refusal path. `message_ids`
                    exposes every message it sent (the live one plus each extra a split answer took),
                    which the router hands to `Assistant.link_messages` — a reaction can land on any
                    of them.
    format.py       compose_answer/footer/NO_ANSWER (non-streamed reply, e.g. scheduling) + LLM markdown
                    → Telegram MarkdownV2 via telegramify-markdown; size-split; plain fallback
    transcribe.py   voice file → text (Transcriber; ElevenLabs Scribe v2, language auto-detected; provider-swappable)
    speak.py        text → voice (Speaker; Sonnet re-voices the markdown reply for speech — Haiku derailed,
                    treating the text as a prompt to answer — then ElevenLabs TTS → Ogg/Opus). Voice id +
                    models are module constants (easy to swap); neutral female by default

  assistant/        the main agent — composition root
    assistant.py    Assistant.reply(conversation_id, text) -> AgentExecuteResult; thin TG↔agent layer (the chat layer formats the result via chat/format.compose_answer)
    conversations.py Conversations — registry: builds each chat's agent once and caches it (main model
                    `MAIN_MODEL = claude-opus-5`; sub-agents pick their own in agents.yml)
    conversation.py Conversation — one chat's reused agent + history + scratchpad; runs one reply (lock-serialized)
    history.py      MongoMessageHistory — transcript in Mongo `conversation_turns`, one doc per turn (soft-deleted when pruned); turns are `MongoTurn` (baski `Turn` + `created_at`); format_for_api strips thinking blocks from settled turns (Opus 4.5+ bills replayed thinking), marks the prompt-cache breakpoint on the last turn (baski `mark_cached`), and stamps each `[Turn N]` marker with the turn's absolute UTC send-time on the first turn and after a >1h gap (`_turn_marker`) so the model can judge recency — absolute (never relative) to stay byte-stable in the cached prefix, normalized via baski `as_utc`; the volatile `[Context: N% used]` footer rides after the breakpoint via `context_status()`; `truncate()` only RECORDS each call's size (via baski `effective_input_tokens`, incl. the cached prefix — raw `input_tokens` is shrunk by caching), and nothing drops a turn: **the transcript only grows, and what NARROWS is the view of it.** Past `_PAYLOAD_RETENTION` a turn is rendered as its words alone (`MongoTurn.said()` — no `tool_use`, `tool_result`, attachments or thinking; those were 45% of the live context when measured, against 16% for the conversation itself), and a turn whose whole content was tool machinery renders as nothing, marker included. Before that it goes whole (`MongoTurn.rendered()`), because a follow-up reaches into the output of the exchange it follows ("show me the second one you found") and 85% of the owner's messages arrive within the hour. That split is a VIEW: the turn and its Mongo document keep everything, so widening the window sends it all again, and nothing here can lose anything. **A turn leaves for good only when the agent deletes it** — `delete_turns` (its `prune_transcript`), soft-deleted on `flush()`; `deleted_at` and `message_ids` stay the only fields that ever change on a stored turn. `add_photo`/`add_document` append a user image/PDF message the model reads natively; its base64 block is persisted like any other, so the image survives a reload and is sent for as long as the window holds it. `link_messages` stamps the Telegram message ids the answer was delivered in onto the newest turn (called by the chat router AFTER `flush()` — it deliberately does not upsert, so it can't create a turn doc missing its audit fields); `TurnLookup` is the reverse read, message → turn, used by the reaction recorder
    judge_prompt.py NISSE_JUDGE_PROMPT — the rubric handed to the judge as `instructions=`. Grades two
                    axes: COMPLETENESS (did the reply deliver the ask) and HONESTY (flattery in place of
                    an assessment · a verdict on a one-sided account · a conclusion put in the owner's
                    mouth · a whole brief dumped into one `retrieval` call). Warmth/emoji and argued
                    agreement are explicitly NOT flattery — without those carve-outs the judge redoes
                    ordinary replies for tone. Change it only behind the `replay-traces` harness
    prompt.py       base system prompt (effective = base + curator overlay from Mongo)
    toolset.py      assembles tools: always-on core + code skills + learned skills

  memory/           LONG-TERM MEMORY: durable owner-facts + the recall_save/recall_read/recall_edit/recall_forget tools
    store.py        Memory model + MemoryStore (Mongo `memories`, short-id CRUD: add/overwrite/set_body/soft_delete)
    tools.py        recall_save · recall_read · recall_edit · recall_forget; index injected via the read tool's user_message()
    recall.py       (future) Active Memory — bounded pre-reply recall over LongTerm

  lists/            ARTIFACT TIER: named mutable lists (shopping/todo/watchlist) — NOT memory
    store.py        ItemList + ListStore (Mongo `lists`, one doc per (conversation_id, name); case-folded name, deduped items, in-place edit, soft-delete)
    tools.py        list_edit (add/remove/clear in one call) · list_show; list-name index injected via list_show's user_message()

  reactions/        RAW SIGNAL: the owner's emoji reactions, stored as they happened
    store.py        Reaction + ReactionStore (Mongo `reactions`, append-only, scoped per conversation).
                    Telegram sends the whole new reaction set on every change, not a delta, so a record
                    keeps both sides (`previous` → `current`); an empty `current` is a reaction taken
                    back. The nightly curator reads this store — the LAST record per turn is its
                    current state, so a retracted 👍 never reads as still standing.
                    Each record also carries the `turn_id` the reacted message came from
                    (`None` for a message no turn produced — a transcript echo, a `/lists` view, an
                    error notice): a reaction grades a turn, and the message→turn link is knowable
                    only while the reply is being sent, so it is resolved once at write time rather
                    than left for a reader that could no longer reconstruct it. What an emoji MEANS
                    is still undecided (owner's call) — the curator reads the emoji plus the
                    conversation around it, and no polarity table is hardcoded.

  prompts/          living system-prompt fragments the bot maintains, per conversation, by type
    store.py        Prompt + PromptType(StrEnum) + PromptStore (Mongo `prompts`, one doc per (conversation_id, prompt_type), overwritten in place)
    tools.py        update_core_memory — the always-on CORE MEMORY block (behaviour rules + owner identity + current focus); injected into the system EVERY turn via the tool's async system_prompt(); edited like a list (add/remove whole lines in one call, mirrors list_edit + shared match_unique) so the agent touches only named lines and never rewrites the block wholesale, size-capped so it stays lean

  scheduling/       self-invocation: one-off reminders + recurring routines (webhook mode only)
    store.py        ScheduledTask + ScheduleStore (scoped, for tools) + FireStore (runner, by id: claim/reschedule/mark_done)
    tools.py        remind · schedule_routine · cancel_schedule (injects active-schedule list); agent gives UTC, asks owner's TZ
    service.py      SchedulingService.enqueue_fire (reuses baski CloudTasksScheduler) + LoggingScheduler stand-in
    runner.py       ScheduleRunner.fire — CAS-claim → re-arm if recurring → Assistant.reply → send
    router.py       POST /schedule/fire — Cloud Tasks worker (mounted via add_webhook_routes)

  search/           SerpApi search tools — 15 leaf tools over baski's SerpApiClient
    serp_tool.py    SerpTool base (params→request→render) + shared format_hits (token-lean Markdown)
    tools.py        google_search · google_ai_answer · google_maps_search · google_news · google_events ·
                    amazon_search→amazon_product · youtube_search→youtube_transcript · google_jobs ·
                    google_maps_search→google_maps_reviews · google_flights · google_hotels ·
                    google_finance · google_scholar (last 5 are research-only: registered but off
                    MAIN_TOOLS, for the research sub-agent's fatter roster)
                    (discovery→detail chains share an entity id; design: docs/serpapi-search-tools.md)

  subagents/        configurable sub-agents (agents-as-tools) — configs seeded in Mongo per chat
    store.py        SubagentConfig + SubagentStore (Mongo `subagents`, scoped; save() records the
                    config it replaced — a sub-agent's prompt IS its behaviour)
    tools.py        subagent_list / subagent_save — runtime roster management (CURATOR-ONLY, never
                    in MAIN_TOOLS): a save is validated against the live registry and an allow-list
                    of models before it can break the next conversation build
    tool.py         SubagentTool — wraps one config as a delegating Tool; runs a fresh isolated Agent
                    (own model/tools/judge/context) on the pinned prompt, returns its answer. Builds
                    its tools through the shared `deps.tools` registry (same as the main agent). A
                    sub-agent may delegate to a sibling — ONE level deep (children get no siblings;
                    delegation ≡ has siblings); a two-level research pipeline (researcher → retrieval)
                    (design + deviations: app/subagents/CLAUDE.md; research: docs/orchestrator-subagent-architecture.md)
    hypothesis_tree.py  add_hypothesis/update_hypothesis over one ephemeral per-run tree — the
                    researcher's living investigation record, injected every turn (NOT a Mongo store)
    registry.py     TOMBSTONE — the tool registry moved to `app/tools/`; delete this file

  curator/          nightly self-maintenance agent (off the request path) — SHIPPED
    evidence.py     the day's turns folded into EXCHANGES (owner message + final answer + the
                    reactions on it, resolved via turn_id); a `[Запланировано]` self-prompt is
                    flagged, not counted as owner input
    classify.py     one offline call labelling each owner message (request/praise/rephrase/
                    rejection/correction/clarification/directive/social) — taxonomy from
                    Don-Yehiya et al. / arXiv:2507.23158, NOT invented; noisy by design, so a
                    label is a lead to verify, never a trigger
    prompt.py       NISSE_CURATOR_PROMPT + CURATOR_JUDGE_PROMPT — the feature itself
    curator.py      collect → classify → agent (inside acting_as(CURATOR)) → count revisions →
                    record the run → message the owner the report
    store.py        CuratorRun + CuratorRunStore (`curator_runs`); an idle pass is recorded too
    router.py       POST /curate — Cloud Scheduler nightly (empty body = every active chat)
                    (design + verified behaviour: docs/curator.md; app/curator/CLAUDE.md)

  tools/            the process-wide TOOL REGISTRY both the main agent and sub-agents build from
    registry.py     ToolRegistry (name→factory) + ToolRegistrar Protocol — generic, tool-agnostic
    wiring.py       build_tool_registry() — calls each domain's register_tools() (ownership by
                    domain). MAIN_TOOLS lives in app/assistant/. See app/tools/CLAUDE.md.
    (future: more nisse-specific leaf Tool classes here — gmail·calendar·perplexity, one per file;
    + external MCP servers as an optional secondary tool source — hybrid)

  skills/           code skills — dev-authored bundles (Python, may wrap a sub-agent)
    research/       research SUB-AGENT (own Agent + search tools)
      agent.py      the sub-agent loop
      skill.py      delegate-to-research Tool exposed to the main agent
                    (learned skills are data specs in Mongo, not code here)
                    NOTE: the generic, data-driven version of this now ships in `subagents/` —
                    a research sub-agent is just a seeded SubagentConfig, no code.
```

`skills/` is design intent (not built yet); the sections below describe it. Shipped today: `chat`,
`assistant`, `memory`, `lists`, `prompts`, `reactions`, `scheduling`, `search`, `subagents`, `curator`,
`tools`, `shared`. The
LLM-as-judge now lives in **baski** (`baski.agents.Judge`/`GeminiJudge`), wired here via `CoreDeps.judge`
→ `AgentConfig.judge` — not a local `app/judge/`. baski owns the MECHANISM (the Gemini call, the `Verdict`
schema); nisse owns the POLICY — every construction site passes `instructions=NISSE_JUDGE_PROMPT`
(`assistant/judge_prompt.py`), so grading rules are changed here, never by editing the library's default.

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
  so chats never cross. baski is persistence-agnostic — the scope is bound in the domain's tool
  factory `(deps, conversation_id) -> list[Tool]` (see Dependency wiring). **A tool talks to
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
  intermediates and log `extra` fields. Keep `reply` / `execute` short orchestrators.
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

## Dependency wiring — a process-wide tool registry (`app/tools/`)

`CoreDeps` (`shared/deps.py`) holds the clients + services built once in `backend.py` (http,
anthropic, database, playwright, bucket, scheduler, schedule_endpoint, judge, `bot`, `questions`)
**plus the tool `registry`** — a `ToolRegistry` (name→factory) built at startup by
`build_tool_registry()`. `bot` is the aiogram client for the rare tool that messages the owner
directly (`ask_user`) — required, since the probe fakes one rather than going without. `questions` is
the one `PendingQuestions` registry per process: `ask_user` parks a question on it and the chat
router looks there for one to answer, so the two sides only meet if they hold the same object.

**Each domain registers its own tools** (ownership by domain, like routers). A domain exposes a
factory `(deps, conversation_id) -> list[Tool]` and a `register_tools(registrar: ToolRegistrar)` that
names it — `search.register_tools` (every web tool, one explicit line each), `memory` / `lists` /
`scheduling` / `prompts`, `subagents.register_tools` (the `hypothesis_tree`), `chat.ask` (`ask_user`,
the transport-coupled clarifying-question tool). `app/tools/wiring.py` `build_tool_registry()` just
calls each domain's `register_tools`. To add a tool: write its factory + `register(...)` line in the
owning domain.

**Both agents build their ToolSet through the SAME registry** — no per-agent duplication:
- main Assistant: `deps.tools.build(MAIN_TOOLS, deps, conversation_id)` — `MAIN_TOOLS` lives in
  `app/assistant/conversations.py` (the Assistant owns its spec): general web + state tools, NOT the
  specialized SerpApi leaves, NOT the researcher-only `hypothesis_tree`.
- sub-agent: `deps.tools.get(name)` per `config.tool_names` (falls through to sibling delegation).

"Which agent gets which tool" is the caller's spec (a name list), not a flag on the tool. Only two
loop-bound primitives are still wired by hand in `Conversations._build`: the short-term scratchpad
(handed to `Conversation`) and `DeleteMessagesTool` (needs the agent's live history). The scheduler
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
make curate U=<id> [DAYS=n] [DRY=1]   # one curator pass: evidence, every change with its `before`, the report
make revisions U=<id> [RUN=<id>]      # the change history — who changed what, and what it replaced
```

- **Injected context** is the ground truth for what the model saw — read it first.
- **`=== ASKED THE OWNER ===`** counts the `ask_user` questions the agent chose to raise. The probe
  passes a fake bot that taps the first option through `questions.resolve_tap`, so a clarifying question doesn't
  hang the run and multi-select takes the real toggle-then-Done path. Tuning when the agent asks vs
  guesses means running several probes on genuine forks AND on unambiguous controls — cases in
  `docs/ask-test-cases.md`. One run is noise.
- **`U=` is the conversation id** (an int). Testing recall/contradiction? Use a *fresh* `U=`: in the
  same conversation the fact is still in the transcript, so the long-term path never runs.
- The probe shows what the agent *did*; `make memories`/`make turns` show the durable *result* in Mongo.
- **Sub-agent runs:** the probe's tool-call list shows only the main agent (a `retrieval`/`researcher`
  call, not what the child did inside). The child traces persist locally too (probe sets the trace-sink
  on `CoreDeps`); walk the whole delegation tree with
  `uv run python .claude/skills/analyze-traces/trace_tree.py <trace_id>` (main → researcher → retrieval
  → leaf tools, via baski's `sub_trace_ids`). To exercise a sub-agent, seed it first with
  `make seed U=<id>` (definitions in `app/subagents/agents.yml`) and probe the **same** `<id>`
  (configs are per-conversation).

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
cases doc (`docs/memory-test-cases.md`, `docs/history-test-cases.md`, `docs/judge_test_cases.md`,
`docs/ask-test-cases.md`). A
task isn't done until you have: added scenarios covering the new behavior, run them, AND re-run the
related existing scenarios to confirm no regression. New feature → new cases doc on the same pattern.

For the **judge**, the regression harness is the `replay-traces` skill — it re-grades catalogued
production traces (FP/FN cases in `docs/judge_test_cases.md`) plus two synthetic probes (`depth_probe`
for completeness, `sycophancy_probe` for honesty) through the live rubric, 3× each. Run all three
before/after any `NISSE_JUDGE_PROMPT` change: a rule added on one axis is exactly how the other
silently regresses, and a redo costs a full regeneration the owner sees as a near-duplicate.

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

## Self-maintenance (nightly curator) — SHIPPED

One background agent, nightly only (Cloud Scheduler → `/curate` at 04:00 PT); no per-turn reflection.
It reviews the day's exchanges plus the owner's reactions, labels what each owner message was doing,
and maintains **core memory**, **memories**, **lists**, and **sub-agent configs** — through the same
tools the live assistant uses. Learned-skill specs are still unbuilt (they wait on `skills/`).

Every content-losing mutation appends to `revisions` with the text it replaced, attributed to the
run (`app/shared/revisions.py`) — runtime-editable state lives in **Mongo, never in code**. The pass
ends by messaging the owner what it changed and on what evidence; a change the owner cannot see is
one they cannot trust. Full design, research grounding, and the three rules that keep it from
drifting (quote the owner · recurrence before a standing rule · praise proves nothing):
`docs/curator.md`.

## baski building blocks (don't reinvent)

`telegram.server.TelegramServer` · `telegram.receptionist.Receptionist` ·
`telegram.history.ChatHistory` · `telegram.storage.UsersStorage` ·
`server.AppConfig` · `agents.{Agent,Tool,ToolSet,MessageHistory,
ShortTermMemory,TraceCollector}` · `clients.{SerpAPIClient,PlaywrightClient}` ·
`primitives.{datetime,json,unique_id}` · `pattern.retry` · `map_async`.
