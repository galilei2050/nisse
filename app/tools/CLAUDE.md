# tools/ — the process-wide tool registry

One `ToolRegistry` (name→factory), built once at startup, that both the main Assistant and every
sub-agent build their `ToolSet` from. Like an HTTP router maps a path to a handler, this maps a tool
name to a factory. Removes the old hand-assembled, per-agent tool wiring.

## Shape

- `registry.py` — `ToolRegistry` (generic, tool-agnostic): `register(name, factory)`, `get(name)`,
  `build(names, deps, conversation_id)`. `ToolFactory = Callable[[CoreDeps, int], list[Tool]]` — a
  factory takes `(deps, conversation_id)` and returns the tool(s) for that name (a list, so one name
  can yield several — the four memory tools, the hypothesis-tree pair). The registry imports **no**
  specific tool, so it isn't the place that "knows all tools".
- `wiring.py` — `build_tool_registry()` registers every tool's factory by name, one line each (the
  composition layer, like `backend.py` collecting routers) + `MAIN_TOOLS`, the main agent's spec.

## How it's used

- Built once in `backend.py` (`build_tool_registry()`) and stored on `CoreDeps.tools`, so it rides
  `deps` everywhere — like `judge`/`scheduler`. The probe builds its own the same way.
- **main Assistant**: `deps.tools.build(MAIN_TOOLS, deps, conversation_id)` (`Conversations._build`).
- **sub-agent**: `deps.tools.get(name)` per `config.tool_names` (`SubagentTool._resolve_tools`).

## Design facts

- **Factory signature is `(deps, conversation_id)`, not `(deps)`.** Conversation-scoped tools (memory,
  lists, scheduling, core memory) build a store bound to the chat, so they need `conversation_id`;
  process-level tools (search leaves, the hypothesis tree) ignore it. One uniform signature keeps the
  registry simple.
- **Audience is the caller's spec, not a flag.** "Which agent gets which tool" is the name list each
  caller passes — the main agent's `MAIN_TOOLS` (everything bar the researcher-only `hypothesis_tree`)
  and each sub-agent's `config.tool_names`. There is no `for_main`/`for_subagent` flag on a tool.
- **Factories live in their domains** (`memory.memory_tools`, `search.search_leaf`, …), registered
  from `wiring.py` — so a domain owns how its tools are built, and the registry class stays generic.
- **Two tools are NOT registered**, by nature: the short-term scratchpad (its instance is handed to
  `Conversation` to clear per reply) and `DeleteMessagesTool` (needs the agent's live history) — both
  wired by hand in `Conversations._build`. Sub-agents themselves are data-driven (Mongo, per chat),
  resolved in `SubagentTool`, not registered here.
