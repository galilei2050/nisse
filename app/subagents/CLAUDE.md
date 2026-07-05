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
  returns `result.response`, raising if it's `None` (no silent empty answer).

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
