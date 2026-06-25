# Core memory — the always-on block (design)

> Canonical rationale + the core-vs-recall **routing rule** now live in `app/memory/CLAUDE.md`.
> This file is the original design note (data model, async per-turn `system_prompt()`, the tool).
> Refinements since: named **Core Memory**, three loose sections (BEHAVIOUR / ABOUT THE OWNER /
> CURRENT FOCUS), a hard size cap (`_CORE_BUDGET`), operational projections (store `timezone`, not
> "lives in X"), and the `preference` memory category removed.

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
freeform prompt the bot maintains. `core_memory` is the first type; the collection is extensible
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
- `PromptType(StrEnum)`: `CORE_MEMORY = "core_memory"` for now. New types add an enum member.
- `PromptStore(database, conversation_id=…)` — mirrors `MemoryStore`: scoped per chat so prompts
  never cross conversations. Surface: `get(prompt_type) -> str | None`, `set(prompt_type, content)`
  (upsert). No soft-delete needed — a type is overwritten, not versioned.

`core_memory` content = general info about the owner + how they want the bot to behave. One
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

`CoreMemoryTool` (`update_core_memory`), holding a `PromptStore`, mirrors `RecallMemoryTool`
(which owns the memory index): it both injects the content and edits it.

It both injects the content (`system_prompt()`, read live every turn) and edits it (`execute`).

**Patch-in-place**, not overwrite-whole (decision reversed — see below). `execute(old, new)` mirrors
`recall_edit`: append `new` when `old` is empty, replace `old` with `new`, or remove `old` when `new`
is empty; an `old` that doesn't match verbatim changes nothing and echoes the current block to retry
against. The size cap applies to the *result*. Tool guidance: persist standing rules about behavior,
address form, formatting, or identity that shapes behavior; CORE is owner-knowledge only — agent
operating procedure (context pruning, tool workflows) does NOT belong here. Keep it tight — pure
per-turn overhead, a rulebook not a fact dump (discrete facts still go to `recall_save`). No
`Conversation.reply` change is needed — the tool owns it end to end.

### Why patch replaced overwrite-whole

The original choice (decision #2 below) was overwrite-whole, to avoid fragile `old`-string matching.
Real traffic showed that cost dominated the benefit: every rule addition resent the full ~2400-char
block, repeatedly tripped the cap (~⅓ of `update_core_memory` calls rejected, forcing a
trim-and-resend round-trip), and the lossy hand-compression dropped or weakened standing rules — the
exact "rules don't stick" failure this store exists to fix. `recall_edit` already proves patch works
in this codebase (its verbatim-match-or-echo fallback is safe and the agent recovers in one retry),
so core memory now uses the same shape: the agent appends one line instead of rewriting the block.

## Relationship to `memories`, and migration

`core_memory` supersedes the `preference` category. After this lands:
- `MemoryCategory` keeps `fact` and `event` (discrete, recalled on demand); `preference` retires.
- One-off migration: fold the 5 existing live `preference` memories (communication style, assistant
  name/address, movie preferences, memory-usage pattern) into the seed `core_memory` content for
  conversation `112991176`, then soft-delete those memory rows.
- `remember`'s guidance drops the `preference` branch; behavioral/identity → `update_core_memory`.

## Bootstrapping & empty state

No row yet → `set_system_extra("")` → system is the base only. The document is created the first
time the owner states a standing preference and the model calls `update_core_memory`.

## Decisions to confirm

1. **Async per-turn `system_prompt()` refactor** (recommended) vs a narrower workaround.
2. ~~**Overwrite-whole** editing vs patch/append semantics.~~ **Resolved: patch/append** — overwrite-whole
   was tried and reversed once real traffic showed the resend-churn / cap-rejection / lossy-compression
   cost (see "Why patch replaced overwrite-whole" above).
3. **Retire `preference`** memory category + migrate (recommended) vs keep both stores.

## Build order (two PRs, baski first — `check-baski` + `@main` pin)

1. baski: make `Tool.system_prompt()` + `ToolSet.system_prompt()` async; assemble the agent system
   per turn; update the two baski impls. Merge.
2. nisse: `prompts` collection + `PromptStore`, `PromptType`, `CoreMemoryTool` wired into the toolset,
   the 5 nisse `system_prompt()` impls gain `async`, migrate `preference` memories, update
   `app/CLAUDE.md` + this doc.
