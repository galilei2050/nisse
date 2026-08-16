# tools/ — the process-wide tool registry

One `ToolRegistry` (name→factory), built once at startup, that both the main Assistant and every
sub-agent build their `ToolSet` from. Like an HTTP router maps a path to a handler, this maps a tool
name to a factory. Removes the old hand-assembled, per-agent tool wiring.

## Shape

- `registry.py` — `ToolRegistry` (generic, tool-agnostic): `register(name, factory)`, `get(name)`,
  `build(names, deps, conversation_id)`, and `catalog(deps, conversation_id)` — every name mapped to the
  `one_line` of each tool it yields, for a caller that has to CHOOSE names rather than use them (the
  curator picking a worker's roster). `ToolFactory = Callable[[CoreDeps, int], list[Tool]]` — a
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
- `curator.register_tools` — `transcript`, the pass's read past its review window (curator-only, and
  refused in a sub-agent's `tool_names` for the same reason `judge_rules` is).
- `chat.ask.register_tools` — `ask_user` (needs `deps.bot`); `browser.register_tools` — `browser`,
  registered and held by nobody.

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
- **Two tools are wired BY HAND**, by nature, because the main agent's copy is bound to its loop: the
  short-term scratchpad (its instance is handed to `Conversation` to clear per reply — the `short_term`
  registry name exists too, for sub-agents that want their own) and `DeleteMessagesTool` (needs the
  agent's live history). Both in `Conversations._build`. Sub-agents themselves are data-driven (Mongo, per chat),
  resolved in `SubagentTool`, not registered here.
- **Registration is not a grant.** Holders are `MAIN_TOOLS` plus each conversation's `tool_names` in
  Mongo — so who holds what is runtime data no file here can state truthfully. What registration buys is
  that a name is *valid* in a `tool_names`, which is the precondition for the nightly curator granting
  it (`subagent_save` validates against this registry). `browser` (`app/browser/`) is the case that made
  the distinction matter; its open defects are registered in `docs/browser-actions.md`.
- **A factory must be cheap and side-effect-free to construct.** `ToolRegistry.catalog` builds every
  registered factory to read each tool's `one_line`, so a constructor that reads a required env var or
  opens a connection would break the curator's roster read — a tool it does not even hold. Do the work
  in `execute`, not in `__init__`.
