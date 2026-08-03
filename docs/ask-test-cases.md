# ask_user — test cases

Expectation-first scenarios for the clarifying-question tool. Run them before and after ANY edit to
`AskUserTool.description` or to the "Act, don't ask PERMISSION" rule in `NISSE_SYSTEM_PROMPT` — those
two strings are the only thing steering this behaviour, and they pull against each other by nature.

```
make probe U=<fresh id> MSG="<the prompt below>"
```

Read `=== ASKED THE OWNER === N` at the end of the output; the questions themselves are in
`=== TOOL CALLS ===`. Use a **fresh** `U=` per run — core memory learned in an earlier run (city,
timezone) removes the very fork the case is testing.

**One run is noise.** The count varies run to run at fixed settings; a case is a regression when it
flips across repeats, not when it moves by one on a single run.

## Forks — the agent must ask (expected: ≥1)

| # | Message | The owner's call it cannot know |
|---|---------|--------------------------------|
| 1 | Забронируй мне столик на ужин в субботу | city, cuisine, party size, time |
| 2 | Напомни завтра позвонить в клинику | the time |
| 3 | Подбери подарок сестре на день рождения | budget, her taste |
| 4 | Составь мне план на выходные | city, what "a good weekend" means to him |

Case 2 is the sharpest: the agent has a working `remind` tool, so guessing is one call away and
invisible — it fires at an invented hour and looks like success. Watch for `remind` being called
with a `fire_at` no one chose.

## Controls — the agent must NOT ask (expected: 0)

| # | Message | Why asking would be wrong |
|---|---------|---------------------------|
| 5 | Добавь молоко и хлеб в список покупок | nothing is missing; just do it |
| 6 | Сколько сейчас стоит биткоин? | a checkable fact — look it up, don't ask |

These matter more than the forks. Pushing the agent toward asking is easy; the failure it buys is an
assistant that interrogates the owner over a shopping list. Any edit that raises the fork count and
also raises these is a regression, not an improvement.

## Recorded runs (2026-08-02)

| Case | Before | After |
|------|--------|-------|
| 1 restaurant | asked 1, then listed the rest **in prose** at the end of the answer | 1–2, all before searching |
| 1 restaurant (repeat) | **0** — listed everything in prose, tool untouched | 2 |
| 2 reminder | **0** — invented 10:00 local and set the reminder | 1, before `remind` |
| 3 gift | 1 | 1–2 |
| 4 weekend | 1 | 1 |
| 5 shopping list | 0 | 0 |
| 6 bitcoin | 0 | 0 |

The two failure modes the "after" column had to remove: a value substituted for the owner's (case 2)
and a question parked in the reply text where it reaches him only after the work is already done
(case 1).

## Not covered here

How an answer gets back once asked — a tap, a typed reply, "None of these", a timeout — is pinned by
`tests/backend/test_ask.py`, which drives the real keyboard payloads through the real state machine.
The probe's fake transport always taps the first option, so it measures the DECISION to ask, never
the answer path.
