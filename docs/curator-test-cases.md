# Curator test cases — capability gaps

Expectation-first scenarios for the nightly pass, written **before** the run, per the project's testing
rule. The pass is a real Opus run over real stores, so there is no unit test for the judgement: the
verifier is `make curate U=<id> [DAYS=n]`. `DRY=1` stops after the classification and never reaches the
curator, so it shows the evidence but says nothing about what the pass decides.

Run against a **throwaway conversation id**, not the owner's — the pass edits live stores. To reproduce a
real window safely, copy its turns to a sandbox id, `make seed U=<sandbox>` to plant the workers, then
`make curate U=<sandbox>`. Between runs, clear the sandbox's `prompts` / `memories` / `lists` /
`subagents` / `revisions` / `curator_runs`: a second run otherwise reads the first run's edits and the
prompt's own warning applies ("your own edits are already in what you see").

## CG-1 — a capability nobody has (the SFO parking window, 2026-08-09)

**Evidence** (conversation 112991176, turns 2402-2408; the owner planning a drive to SFO):

| turn | what happened |
|---|---|
| 2402 | owner: `SkyPark SFO (off-airport) - с какого по какое брать` |
| 2404 | owner pastes the site back: `SkyPark is not available for the selected dates. Showing other parking options below.` |
| 2406 | owner: `У тебя разве нет агента через браузер верифицировать информацию?` + the lot list, by hand |
| 2407 | assistant: `Про агента честно: у меня есть саб-агент, который ищет и читает страницы, но он не заполняет формы бронирования и не видит real-time наличие мест.` |

All three tells of a capability gap are present at once: the assistant states what it cannot do, the
owner asks whether the capability exists, and the owner performs the step by hand.

**Must happen**

- The pass reads the roster (`subagent_list`) and reaches the shelf printed under it.
- It distinguishes `browse_website` ("Browse and extract content from any website") from `browser`
  ("Open a URL in a real browser you can act in… Click… Type into…"). Concluding the capability was
  already present because `retrieval` holds `browse_website` is the measured failure this case exists for.
- The roster changes: either `browser` is added to a worker's `tool_names`, or a new worker is created
  for acting on pages. Both are correct answers; which one is the pass's call.
- The report names the change and quotes the owner.

**Must NOT happen**

- A core-memory line ordering the assistant to check live availability, with no tool that can. This was
  the outcome of the pre-change run (2026-08-09, sandbox): a rule the assistant cannot obey, which the
  owner reads as a promise.
- A memory recording that the agent "cannot see real-time availability" — a limitation stored as a
  durable fact is what the prompt's do-not-capture list is for. Granting the tool is the fix.
- `ask_user` in any `tool_names` (it blocks on a tap; a scheduled worker would wait out 300s).
- A worker prompt containing instructions for driving the browser tools. Each tool states its own rules
  and the worker re-reads them every turn; a copy in the prompt is what goes stale.

**Measured before the change** (2026-08-09, sandbox 5150001, $0.3061): found the failure and quoted both
signals, then left the roster alone — *"у `retrieval` уже есть `browse_website`… Инструмент был на
месте"* — and wrote the impossible core-memory rule. That run is the baseline this case is graded against.

## CG-2 — a quiet night must stay quiet

**Evidence.** Any window with no correction, no contradiction and no capability tell.

**Must happen.** No roster change, and the report says so in one line. A capability gap needs the same
evidence as anything else; inventing one costs a worker nobody needed and a bill on every delegation to it.

## CG-3 — a complaint about depth, not about capability

**Evidence.** The owner calls a researched answer shallow, and the tools to do it better were all held.

**Must happen.** The fix lands on the worker's prompt or description (looked-for item 5), NOT on
`tool_names`. This is the case that keeps the capability lever from becoming the answer to everything.
