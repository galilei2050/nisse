# subagents/ — configurable sub-agents (agents-as-tools)

Per-conversation sub-agent configs in Mongo `subagents`, each exposed to the main agent as one
delegating `Tool` (string prompt in → string result out). A sub-agent is just a `baski.agents.Agent`
with its own toolset/model/system-prompt/judge/context, wrapped by `SubagentTool`.

## Shape

- `store.py` — `SubagentConfig(NisseDbModel)` (seven required config axes + `conversation_id`) +
  `SubagentStore` (scoped `list()` for the build; `save()` is seed-only; `ensure_indexes` unique on
  `(conversation_id, name)`).
- `registry.py` — `TOOL_REGISTRY` (tool `.name` → factory) + `build_tools(names, deps)`. Holds ONLY
  read-only web/browse leaves — the same set `_build_web_tools` gives the parent. It is the **child
  whitelist**: a config naming anything else fails loud at build. No state-writing / send / sub-agent
  tool is in it, so a child can't write shared state or recurse.
- `tool.py` — `SubagentTool`: per-config `name`/`description` (instance attrs, shadowing the class
  defaults — one class, N configs); `execute` runs a fresh isolated `Agent` on the pinned prompt and
  returns `result.response`, raising if it's `None` (no silent empty answer). `_resolve_tools` maps
  each `tool_names` entry to live tool(s): a registry web leaf, the `hypothesis_tree` pair, or — for
  an orchestrator (`can_delegate=True`) — a child `SubagentTool` (most names give one tool;
  `hypothesis_tree` expands to two).
- `hypothesis_tree.py` — the researcher's living investigation record, edited **node-by-node** with
  two granular tools (`add_hypothesis` / `update_hypothesis`) over one shared `HypothesisTree`, not
  rewritten whole (mirrors the `list_edit`/core-memory idiom: touch one node, don't resend the tree,
  can't drop a node). `update_hypothesis.system_prompt()` injects the whole tree every turn (single
  injection point of the pair), same shape as core memory. **Ephemeral in-instance state** — a fresh
  shared tree per `SubagentTool.execute` run (one investigation), gone after it. No Mongo, no
  conversation scope. `build_hypothesis_tree_tools()` makes the shared tree + its two tools.

## Depth-1 nesting (two-level research pipeline)

A sub-agent may delegate to *another* sub-agent, but only ONE level deep — an orchestrator delegates,
its children are leaves. Modelled as a boolean, not a counter:

- Top-level `SubagentTool`s are built with `can_delegate=True` and `siblings` = every config in the
  conversation (`Conversations._build_subagent_tools`). `can_delegate=True` only *permits* resolving
  a sibling name that appears in a config's `tool_names`; a worker whose names are all registry leaves
  never uses it.
- A child sub-agent is built as a leaf: `siblings={}` + `can_delegate=False`. Those two together cap
  nesting at one level — a child can't see or delegate to anyone.
- A `tool_names` entry that resolves to nothing (unknown registry key, or a sibling name at a level
  that can't delegate) raises at build — a seed error, loud, matching the registry whitelist.

`registry.py`/`build_tools` and `_build_web_tools` are unchanged; only the child-toolset path in
`_resolve_tool` gained the `hypothesis_tree` and sibling-delegation cases.

The seed pattern (`scratch/seed_subagents.py`): `researcher` (orchestrator, `tool_names =
["retrieval", "hypothesis_tree"]`, never searches itself) + `retrieval` (worker, the web leaves).
The researcher owns the hypothesis tree, decomposes the question, delegates each sub-question to
`retrieval`, and synthesizes; `retrieval` answers one self-contained sub-question and returns cited
compression. Methodology encoded in the prompts: `docs/research-subagent.md`.

**v1 has no isolated verifier** (research doc §3.3 — a separate agent given only the cited sources).
Verification hygiene is left to each sub-agent's own completeness judge (`GeminiJudge`, wired per
config). Add a verifier later only if usage shows a need.

## Wiring

`Conversations._build_subagent_tools(conversation_id)` reads the configs and adds one `SubagentTool`
each. Configs are read once at conversation-build; the agent is cached, so a re-seed takes effect on
the next process start (no cache invalidation — not needed for an admin-seeded, rarely-changing set).
`_build_web_tools` and the registry share one source of truth (`build_tools`); the parent gets all
registry keys, a child gets its configured subset.

## Design facts (why it's built this way)

- **The child owns the return-path compression.** A sub-agent's `system_prompt` MUST demand a
  compressed, structured answer — the child's output re-enters the parent's limited context, and
  token volume dominates cost/quality (research: `docs/orchestrator-subagent-architecture.md` §3.2,
  §5). The downward brief (goal / output format / boundaries) lives in `SubagentTool.Input.prompt`'s
  description — the strongest lever available under the owner's fixed single-string interface.
- **`subagents` is a trusted admin surface.** It drives which tools/model/prompts run; seed it from an
  admin script only. Never wire a user-facing writer to it. `tool_names` is validated against the
  registry; `model`/prompts are trusted because the seed channel is.
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
