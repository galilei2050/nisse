# tools/ — the process-wide tool registry

One `ToolRegistry` (name→factory), built once at startup, that both the main Assistant and every
sub-agent build their `ToolSet` from. Like an HTTP router maps a path to a handler, this maps a tool
name to a factory. Removes the old hand-assembled, per-agent tool wiring.

## Shape

- `registry.py` — `ToolRegistry` (generic, tool-agnostic): `register(name, factory)`, `get(name)`,
  `build(names, deps, conversation_id)`. `ToolFactory = Callable[[CoreDeps, int], list[Tool]]` — a
  factory takes `(deps, conversation_id)` and returns the tool(s) for that name (a list, so one name
  can yield several — the four memory tools, the hypothesis-tree pair). Also defines the
  `ToolRegistrar` **Protocol** (just `register`) — what a domain's `register_tools` depends on, so a
  domain never imports the whole registry (nor cycles back through wiring). The registry imports **no**
  specific tool, so it isn't the place that "knows all tools".
- `wiring.py` — `build_tool_registry()`: a thin orchestrator that calls each domain's
  `register_tools(registrar)`, like `backend.py` collecting routers. `backend.py` + probe call it.

## Ownership: each domain registers its own tools

A domain owns HOW its tools are built AND under which names — it exposes a factory `(deps,
conversation_id) -> list[Tool]` and a `register_tools(registrar: ToolRegistrar)` that names them:

- `search.register_tools` — every web tool, one explicit `register(...)` line each (no sweep list):
  the SerpApi leaves + `browse_website`.
- `memory` / `lists` / `scheduling` — register `memory` / `lists` / `scheduling`.
- `prompts.register_tools` — `core_memory` and `judge_rules` (the latter curator-only: it is refused
  in a sub-agent's `tool_names`, see `app/subagents/tools.py`).
- `subagents.register_tools` — the researcher-only `hypothesis_tree`.

`wiring.build_tool_registry()` just calls these in turn; to add a tool, add its factory +
`register(...)` line in the owning domain (nothing in `wiring.py` changes except a new
`domain.register_tools(registry)` call if it's a new domain).

## How it's used

- Built once in `backend.py` (`build_tool_registry()`) and stored on `CoreDeps.tools`, so it rides
  `deps` everywhere — like `scheduler`. The probe builds its own the same way.
- **main Assistant**: `deps.tools.build(MAIN_TOOLS, deps, conversation_id)` (`Conversations._build`).
  `MAIN_TOOLS` lives in `app/assistant/conversations.py` (the Assistant owns its own spec).
- **sub-agent**: `deps.tools.get(name)` per `config.tool_names` (`SubagentTool._resolve_tools`).

## Design facts

- **Factory signature is `(deps, conversation_id)`, not `(deps)`.** Conversation-scoped tools (memory,
  lists, scheduling, core memory) build a store bound to the chat, so they need `conversation_id`;
  process-level tools (web leaves, the hypothesis tree) ignore it. One uniform signature keeps the
  registry simple.
- **Audience is the caller's spec, not a flag.** "Which agent gets which tool" is the name list each
  caller passes — the main agent's `MAIN_TOOLS` (general web + state tools; NOT the specialized SerpApi
  leaves, NOT the researcher-only `hypothesis_tree`) and each sub-agent's `config.tool_names`. There is
  no `for_main`/`for_subagent` flag on a tool. Every web tool is *registered* (so sub-agents may use
  it); the main agent's roster is just the general subset, to keep its per-turn schema lean.
- **Two tools are NOT registered**, by nature: the short-term scratchpad (its instance is handed to
  `Conversation` to clear per reply) and `DeleteMessagesTool` (needs the agent's live history) — both
  wired by hand in `Conversations._build`. Sub-agents themselves are data-driven (Mongo, per chat),
  resolved in `SubagentTool`, not registered here.
