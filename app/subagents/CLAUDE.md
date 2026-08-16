# subagents/ — configurable sub-agents (agents-as-tools)

Per-conversation sub-agent configs in Mongo `subagents`, each exposed to the main agent as one
delegating `Tool` (string prompt in → string result out). A sub-agent is just a `baski.agents.Agent`
with its own toolset/model/system-prompt/judge/context, wrapped by `SubagentTool`.

## Shape

- `store.py` — `SubagentConfig(NisseDbModel)` (eight required config axes incl. `max_turns` — the hard
  cap on the child's loop, passed to `AgentConfig.max_turns` — + `conversation_id`) +
  `SubagentStore` (scoped `list()` for the build; `save()` records the config it replaced and is
  shared by the seed script and the curator; `ensure_indexes` unique on
  `(conversation_id, name)`).
- `tool.py` — `SubagentTool`: per-config `name`/`description` (instance attrs, shadowing the class
  defaults — one class, N configs); `execute` runs a fresh isolated `Agent` on the pinned prompt and
  returns `result.response`, raising if it's `None` (no silent empty answer). `_resolve_tools` maps
  each `tool_names` entry to live tool(s) via the shared registry `deps.tools.get(name)` (e.g.
  `hypothesis_tree` → the add/update pair); a name that isn't a registered tool falls through to
  sibling delegation, else fails loud. The tool catalog itself lives in `app/tools/` (not here).
- `hypothesis_tree.py` — the researcher's living investigation record, edited **node-by-node** with
  two granular tools (`add_hypothesis` / `update_hypothesis`) over one shared `HypothesisTree`, not
  rewritten whole (mirrors the `list_edit`/core-memory idiom: touch one node, don't resend the tree,
  can't drop a node). `update_hypothesis.user_message()` injects the whole tree every turn (single
  injection point of the pair) — the same slot the list and memory indexes use, and NOT
  `system_prompt()`: live state in the first cached block costs the whole prefix on every edit.
  **Ephemeral in-instance state** — a fresh
  shared tree per `SubagentTool.execute` run (one investigation), gone after it. No Mongo, no
  conversation scope. `build_hypothesis_tree_tools()` makes the shared tree + its two tools.

## Depth-1 nesting (two-level research pipeline)

A sub-agent may delegate to *another* sub-agent, but only ONE level deep — an orchestrator delegates,
its children are leaves. Delegation is derived, not flagged: **a sub-agent may delegate exactly when
it HAS siblings.**

- Top-level `SubagentTool`s are built with `siblings` = every config in the conversation
  (`Conversations._build_subagent_tools`). Having siblings only *permits* resolving a sibling name
  that appears in a config's `tool_names`; a worker whose names are all registry tools never uses it.
- A child sub-agent is built with `siblings={}`, so it can't see or delegate to anyone — capping
  nesting at one level.
- A `tool_names` entry that is neither a registered tool nor a delegable sibling raises at build — a
  seed error, loud.

**The live definition is the Mongo document, not `agents.yml`.** The file is a SEED — it plants a
conversation's first copy, and from then on the curator edits the document (`subagent_save` is in
`CURATOR_TOOLS`), so the two diverge and the document is what runs. Measured 2026-08-08 on the owner's
chat: the live `retrieval` prompt carried a whole effort-budget section absent from the file, and
`researcher` a stop-early rule — 1367 vs 1218 chars and 2398 vs 1990. To read what an agent actually
does, query `subagents`.

**Nothing is lost by living in the database.** Every write goes through `SubagentStore.save`, which
records a revision holding the full text before and after — so the history of a prompt is
`make revisions U=<id>`, not `git log`. That is why the prompts are not mirrored back into the file:
the reason to keep them in git would be history, and history is already kept.

**Therefore `make seed U=all` is a REVERT, not a rollout.** It upserts the file over every live
document and silently discards everything the curator learned. Use it for a brand-new conversation
(`make seed U=<id>`), or when a new REQUIRED field would otherwise break reads — and in that case
expect to lose the curator's text unless it is carried into the file first. To change how a live agent
behaves, edit the document (the curator's own path), not the file.

Sub-agent definitions (name, description, prompts, model, tool_names, judge) are seeded from
**`agents.yml`** (`scripts/seed_subagents.py`, upsert on (conversation_id, name)). The intended shape is a `researcher` orchestrator (owns the hypothesis tree, decomposes the
question, delegates each sub-question, synthesizes) over a `retrieval` worker (answers one
self-contained sub-question with cited compression) — but `agents.yml` is what's actually defined.
Methodology behind the prompts: `docs/research-subagent.md`.

**`subagent_list` prints its index in the RESULT, not through `Tool.user_message()`** — the one place in
this app that deviates from the uniform seam. Two reasons: the index here is the whole registry
(`ToolRegistry.catalog` builds every registered tool to read its `one_line`), so a `user_message()` block
would pay for all of it on each of the curator's up-to-40 turns; and both `subagent_save`'s description and the curator's
prompt require a `subagent_list` before any edit, since a save replaces a config wholesale. Nothing
ENFORCES that (`_reject` does not check), but the read is asked for anyway, so the index rides it. Do not copy this for a tool whose index is small — use the seam.

`register_tools` (this package's `__init__.py`) registers the sub-agent-facing registry tools that
aren't plain web leaves — `hypothesis_tree` (the add/update pair) and `short_term` (a fresh
`ShortTermMemory`/`working_note` for holding findings across turns). Both build a fresh instance per run.

**v1 has no isolated verifier** (research doc §3.3 — a separate agent given only the cited sources).
Verification hygiene is left to each sub-agent's own completeness judge (`GeminiJudge`, wired per
config). Add a verifier later only if usage shows a need.

## Wiring

`Conversations._build_subagent_tools(conversation_id)` reads the configs and adds one `SubagentTool`
each. Configs are read once at conversation-build; the agent is cached, so a re-seed takes effect on
the next process start (no cache invalidation — not needed for an admin-seeded, rarely-changing set).
A sub-agent builds its tools through the SAME `deps.tools` registry the main agent uses (`app/tools/`)
— the main agent's spec is `MAIN_TOOLS` (in `app/assistant/`: general web + state tools), a
sub-agent's is its `config.tool_names` (which may name the specialized SerpApi leaves + `hypothesis_tree`).

**The main agent won't delegate without being told to.** With general web tools in `MAIN_TOOLS`, it
answers even hotel/flight lookups itself via `google_search` unless the system prompt tells it to
route specialized/deep work to a sub-agent (`NISSE_SYSTEM_PROMPT`, the "Delegate to your sub-agents"
clause — prod-safe: it only bites when a sub-agent tool is present). Routing is driven by each
sub-agent's `description` (e.g. `retrieval` owns hotels/flights — the only path to `google_hotels`).

**Trace-sink flows via `deps`.** A sub-agent's child `Agent` gets `await_trace`/`local_traces_dir`
from `CoreDeps` (default off → GCS; a probe run sets them so the whole delegation tree persists
locally and links via baski's `sub_trace_ids`). Walk it with `analyze-traces/trace_tree.py <id>`.

## Design facts (why it's built this way)

- **The child owns the return-path compression.** A sub-agent's `system_prompt` MUST demand a
  compressed, structured answer — the child's output re-enters the parent's limited context, and
  token volume dominates cost/quality (research: `docs/orchestrator-subagent-architecture.md` §3.2,
  §5). The downward brief (goal / output format / boundaries) lives in `SubagentTool.Input.prompt`'s
  description — the strongest lever available under the owner's fixed single-string interface.
- **`subagents` is a trusted admin surface.** It drives which tools/model/prompts run. Two writers
  exist, both trusted: the seed script, and the nightly curator through `subagent_list` /
  `subagent_save` / `subagent_forget` (`tools.py`, registered as `subagents` — curator-only,
  deliberately NOT in `MAIN_TOOLS`, so nothing the owner types in chat reaches this write path).
  Retirement is a soft delete and is refused while a live worker names the target in its
  `tool_names`; `make seed` leaves a retired worker retired, since reviving it would undo a decision
  made on the owner's evidence. Never wire a
  user-facing writer to it. A save validates `tool_names` against the live registry and `model`
  against `ALLOWED_MODELS` *before* writing — an unknown tool name would otherwise surface as a
  crash at the next conversation build, taking the whole chat down rather than one tool call. The
  config it replaced is kept in `revisions`, since a sub-agent's prompt IS its behaviour.
- **Stateless & isolated.** Fresh `InMemoryMessageHistory` per call (no warm session — that would
  reintroduce a second state writer, §1.4/§2.2). Each run gets its own trace (baski creates it).
- **When to configure one at all:** only for genuine context-isolation/compression wins (deep
  research, multi-page browsing) — not routine lookups. Multi-agent costs ~15× the tokens of a plain
  chat turn (§5); a single strong tool call usually wins. Reliability/trust over peak cleverness
  (project decision principles).

## Deliberate deviations from the research doc (stated, not hidden)

- §2.1's by-reference artifact channel is NOT built — nisse has no artifact store, so children return
  summary-only (the compression half of §2.1, not the handle half). Fine for research/browse children.
- §3.1's typed brief is a field-description nudge, not schema-enforced fields — the owner fixed the
  single-string I/O. The fan-out/duplication failure §3.1 guards against is structurally unreachable
  for a single-user, sequential, one-tool-per-subagent design.
- Child output returns as a baski `tool_result` block (data, not instructions) — no extra
  untrusted-fencing/scrubbing (the memory tier's `<memory-context>` discipline). Revisit if a child
  ever quotes adversarial content verbatim.
