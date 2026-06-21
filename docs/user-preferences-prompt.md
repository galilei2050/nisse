# Living prompts collection — `user_preference` (design)

## Problem

Behavioral corrections don't stick. The owner says "don't address me by name every message" /
"keep the date format we agreed on" / "you know I live in Santa Clara" — and the bot keeps
violating it. Mechanism (from code): every memory (fact/preference/event) reaches the model only as
an **index of titles** (`RecallMemoryTool.user_message`, `app/memory/tools.py:128`); the body loads
only when the agent chooses to call `read_memory(public_id)`. The agent does not read memories
before every reply, so a standing behavioral rule is never in context when it's needed. Recall-on-
demand is right for discrete facts ("dog named Ruffles") and wrong for always-applies rules.

## Shape

A standing instruction is not a discrete recalled fact — it's a **living document that must be in
context every turn**. Different access pattern (always-on injection vs index + on-demand recall) →
different store. A separate `prompts` collection, keyed `(conversation_id, prompt_type)`, each row a
freeform prompt the bot maintains. `user_preference` is the first type; the collection is extensible
to other prompt kinds later (persona, task templates, curator-learned prompts) without schema change.

## Data model

Collection `prompts`, one document per `(conversation_id, prompt_type)`:

```
conversation_id : int
prompt_type     : str   (PromptType StrEnum)
content         : str   (the prompt text; freeform)
created_at, updated_at  (from NisseDbModel)
```

- Unique compound index `(conversation_id, prompt_type)` — one row per type per chat; upsert by it.
- `PromptType(StrEnum)`: `USER_PREFERENCE = "user_preference"` for now. New types add an enum member.
- `PromptStore(database, conversation_id=…)` — mirrors `MemoryStore`: scoped per chat so prompts
  never cross conversations. Surface: `get(prompt_type) -> str | None`, `set(prompt_type, content)`
  (upsert). No soft-delete needed — a type is overwritten, not versioned.

`user_preference` content = general info about the owner + how they want the bot to behave. One
consolidated document, not a list of records.

## Injection into the system prompt — refactor, not a patch

Requirement: it must be in the **system** prompt (behavioral authority), and **fresh** (an edit
applies on the next reply, no stale cache).

The honest fix is to correct the asymmetry in baski, not to patch around it. Today
`Tool.system_prompt()` is **sync, aggregated once** at `Agent.__init__`, while its sibling
`Tool.user_message()` is **async, called every turn** (`toolset.py:64`). That split is the only
reason the system looks "static" — and it's why a bolt-on like `set_system_extra` (mutating an
already-baked system from outside) would be a crutch. Make the two symmetric instead:

- **baski:** `Tool.system_prompt()` → `async`, default `""`. `ToolSet.system_prompt()` → `async`,
  awaiting each tool (same loop shape as `user_messages()`). `Agent` stops baking the system in
  `__init__`; it assembles `f"{base}\n\n{await toolset.system_prompt()}\n\n{AGENT_LOOP_GUIDANCE}"`
  **per turn** in `_run_turn` (and once in `execute()` for the trace record). baski has no prompt
  caching, so re-assembling each turn is free. Blast radius: base `Tool`, `ToolSet.system_prompt`,
  the agent, and ~7 existing impls that just gain `async` (baski: short_term_memory, delete_messages;
  nisse: remember/edit/forget, scheduling ×2).

With that, a tool's `system_prompt()` is dynamic per turn — so the preference content reaches the
system the same way the memory index reaches the user block: the tool that owns it returns it.

## The tool — one tool owns inject + edit

`PreferenceTool` (`update_preferences`), holding a `PromptStore`, mirrors `RecallMemoryTool`
(which owns the memory index): it both injects the content and edits it.

```python
async def system_prompt(self) -> str:        # injected into system every turn, read live
    content = await self._store.get(PromptType.USER_PREFERENCE)
    return f"## Owner preferences (standing instructions — always follow)\n{content}" if content else ""

async def execute(self, *, content: str) -> str:   # overwrite the whole document
    await self._store.set(PromptType.USER_PREFERENCE, content)
    return "Preferences updated."
```

**Overwrite-whole**, not patch: the current document is in the system every turn, so the model reads
it, applies the correction, and writes the full updated version — no fragile `old`-string matching,
no forget-then-re-add. Tool guidance: persist standing instructions about behavior, address form,
formatting conventions, or identity facts that shape behavior; when correcting, **preserve existing
rules and amend**. Keep it tight — it's pure per-turn overhead, a rulebook not a fact dump (discrete
facts still go to `remember`). No `Conversation.reply` change is needed — the tool owns it end to end.

## Relationship to `memories`, and migration

`user_preference` supersedes the `preference` category. After this lands:
- `MemoryCategory` keeps `fact` and `event` (discrete, recalled on demand); `preference` retires.
- One-off migration: fold the 5 existing live `preference` memories (communication style, assistant
  name/address, movie preferences, memory-usage pattern) into the seed `user_preference` content for
  conversation `112991176`, then soft-delete those memory rows.
- `remember`'s guidance drops the `preference` branch; behavioral/identity → `update_preferences`.

## Bootstrapping & empty state

No row yet → `set_system_extra("")` → system is the base only. The document is created the first
time the owner states a standing preference and the model calls `update_preferences`.

## Decisions to confirm

1. **Async per-turn `system_prompt()` refactor** (recommended) vs a narrower workaround.
2. **Overwrite-whole** editing (recommended) vs patch/append semantics.
3. **Retire `preference`** memory category + migrate (recommended) vs keep both stores.

## Build order (two PRs, baski first — `check-baski` + `@main` pin)

1. baski: make `Tool.system_prompt()` + `ToolSet.system_prompt()` async; assemble the agent system
   per turn; update the two baski impls. Merge.
2. nisse: `prompts` collection + `PromptStore`, `PromptType`, `PreferenceTool` wired into the toolset,
   the 5 nisse `system_prompt()` impls gain `async`, migrate `preference` memories, update
   `app/CLAUDE.md` + this doc.
