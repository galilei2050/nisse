# The curator — nightly self-maintenance

One agent pass, once a night, off the request path — or on demand, when the owner sends `/curate`. It
reads a day of conversation plus the owner's emoji reactions, works out what the owner was *doing* in
each message, and edits the five stores that decide how the assistant behaves tomorrow: core memory,
long-term memories, lists, the judge's added rules, and sub-agents.

The assistant's model is frozen — it cannot get smarter. What it can do is start tomorrow from a
better store. That is the whole ceiling of this feature, and worth stating plainly: the curator makes
nisse better at *this owner's context and procedures*, never fundamentally more capable.

## Why the safety machinery is the feature

An unattended agent editing its own standing rules is exactly the thing the project's decision
principles warn about: **an unverifiable miss is disproportionately costly**, and a change the owner
cannot see is unverifiable by construction. Three properties keep the pass trustworthy:

1. **Every write is attributed and reversible.** The pass wraps its work in `acting_as(CURATOR)`, so
   each store records the text it replaced against that run id (`app/shared/revisions.py`,
   `make revisions U=<id>`). `before` is the copy that would otherwise be gone.
2. **The owner is told.** The pass ends by messaging its report — what changed, on what evidence, and
   what it deliberately left alone. Silent self-modification is the trust-killer.
3. **Recurrence gates durable rules.** A standing rule needs the owner to have shown it more than
   once, or stated it as standing. One irritated message on a bad day is not policy.

## The evidence

`evidence.py` assembles the window in plain code — no model call for what a query already knows.

**A turn is not an exchange.** The transcript stores one turn per API call, so a question that took
three tool rounds is one turn with the owner's words and two with the assistant's narration. Handed
over raw, most entries read as "owner said nothing" and the day looks like the assistant talking to
itself. Turns are folded back into exchanges: one owner message, the *final* answer (the middle turns
are live progress narration), and any reactions that landed anywhere inside.

**Reactions resolve through `turn_id`.** Telegram names only `(chat_id, message_id)`; the link exists
because the chat layer records which messages carried which answer (`MongoMessageHistory.link_messages`).
The reaction log is append-only with the whole set per record, so the *last* record for a turn is its
present state — an earlier 👍 that was taken back must not read as still standing.

**A scheduled self-prompt is not the owner.** A reminder firing enters the transcript as a user
message. Marked as such, so the curator never learns from prompts it wrote itself.

**A window with no owner in it does not get a pass.** That `scheduled` mark is also the run/skip
decision — `Evidence.has_owner_signal` is what `curate()` stops on, and the property says why. The
design point is that this is answerable in the same query that assembles the window: every lever the
pass can pull needs the owner's words, so a night of check-ins answering themselves could only ever
produce a report saying so. Note the reach: reactions count on their own, but only on turns inside
the window, so a tap on an older answer does not by itself earn a pass.

## The classifier

One call over the whole window labels each owner message. It is **offline by design** — an inline
classifier on a single-user bot is a rejected direction (`app/CLAUDE.md`: no cost/latency machinery
without amortization); here the whole day costs one call and serves the only consumer there is.

The taxonomy is not invented. It follows the implicit-feedback ontology of Don-Yehiya et al. (2024),
as densely re-annotated by Liu, Zhang & Choi, *User Feedback in Human-LLM Dialogues: A Lens to
Understand Users But Noisy as a Learning Signal* (arXiv:2507.23158) — positive feedback plus four
negative kinds (rephrase, make-aware-without-correction, make-aware-with-correction, ask-for-
clarification). Three labels are added for what this bot routes on and a *feedback* ontology has no
slot for: `request` (no verdict at all), `directive` (a standing rule), and `social`.

| Kind | What it is | What it is worth |
|---|---|---|
| `request` | a task or question, no verdict | context |
| `praise` | approves the last answer | **nothing** — see below |
| `rephrase` | re-asked the same thing differently | the answer missed, silently |
| `rejection` | "that's wrong", no fix given | a miss, cause unknown |
| `correction` | what was wrong AND what right is | the richest signal |
| `clarification` | asks for what the answer omitted | a gap in the answer |
| `directive` | a rule for future behaviour | core-memory candidate |
| `social` | venting, chat, no task | context |

Three findings from that paper shape how the labels may be used, and they are why nothing here
triggers a change on its own:

- **Praise is a bad learning signal.** Prompts that drew positive feedback scored slightly *lower* on
  quality and higher on toxicity — people praise most warmly when the model went along with a request
  it should have pushed back on. Nothing is promoted on approval alone.
- **Content beats polarity.** *What* was unsatisfactory teaches; a thumbs-down does not. Every label
  carries the owner's exact words and what the miss was about.
- **Automatic labelling is noisy** (~49% on the fine-grained set). A label is a lead to verify
  against the transcript, never a fact to act on — the curator's prompt says so explicitly.

## What the curator may change, and what it may never learn

Its surface is `CURATOR_TOOLS` (`app/curator/curator.py` — the list and the reason for each entry live
there, not here): one writer per store, plus search and page-reading to decide with. No `ask_user` —
the owner is asleep. The writers are the **same tools the live assistant uses**, so there is one write
path per store rather than a parallel curator-only one that could drift; two of them are absent from
`MAIN_TOOLS` because they decide how every later reply is produced or accepted, so only the attributed,
reported nightly pass writes them.

Building a worker is reversible: `subagent_forget` retires one, and because `save` replaces the
document whole without filtering on `deleted_at`, re-saving the name revives it. It exists because
the roster is a routing surface — while a worker the owner called useless is listed, work keeps being
delegated to it. It is refused while a live worker names the retired one in its `tool_names`, which
would otherwise surface as a failed delegation in a live turn rather than at the next build. Retiring
is the answer to a rejected worker; the answer to a gap that cannot be closed is to report it and
build nothing. `make seed` leaves a retired worker retired.

Why it can read the web at all: a **capability gap** is a failure class the behaviour levers cannot
touch. When the day shows work the assistant could not do *at all*, the lever is the roster — grant the
missing tool, or build a worker when the task differs in kind — and deciding which requires knowing what
the capability actually involves, which is a question about the world rather than about the transcript.
The rules themselves are in `NISSE_CURATOR_PROMPT` (`app/curator/prompt.py`); this paragraph exists to
say the class is recognised on purpose, not to restate them.

`judge_rules` is the lever for a failure that core memory cannot fix. When the owner has to repeat a
complaint the core block already covers, rewording that instruction a third time changes nothing —
the answering model read it and went ahead anyway. A line in the judge's rubric REFUSES the finished
answer instead of asking for better behaviour, which is the same reasoning that put the honesty axis
in the judge rather than the system prompt (`app/assistant/judge_prompt.py`). The base rubric stays
in code, deploy-versioned and calibrated against `docs/judge_test_cases.md`; the curator's lines are
appended to it and capped, so a bad one is a line to drop rather than a rubric to reconstruct.

The "do NOT capture" list is adapted from hermes' background-review prompt, and it is the part most
worth keeping intact: environment/setup failures, negative claims about capabilities ("search does
not work"), transient errors that resolved, one-off task narratives, and **its own output**. Each of
those hardens into a permanent self-inflicted constraint the assistant later quotes at itself.

Its judge is its own (`CURATOR_JUDGE_PROMPT`), not the assistant's. The assistant's completeness
rubric grades how fully an answer served a request and would push a maintenance pass toward doing
more work on thinner evidence; the curator's rubric grades the opposite discipline — is every claimed
change backed by something the owner said, and is a quiet night reported as one.

## Change history

`revisions` is append-only: `{collection, target, kind, before, after, actor, run_id}`. Both the
assistant (mid-conversation) and the curator write through it; the actor rides a context variable, so
no tool factory has to thread it.

It is a separate collection rather than a second version in the edited one, because `memories` and
`lists` carry unique indexes that **deliberately** span soft-deleted documents: re-adding a cleared
list revives its document, and a soft-deleted memory keeps its `public_id` reserved. A superseded
copy beside the live one would collide with both rules.

This matches the consolidation literature's default — recency-wins with explicit invalidation, old
state kept for audit, current state unambiguous — and its verdict that eviction is a compliance tool,
not a quality one: good consolidation makes stale facts unretrievable without deleting them.

## Running it

```
make curate U=<conversation_id>            # one real pass: evidence, changes, report
make curate U=<conversation_id> DRY=1      # stop after the classification, change nothing
make curate U=<conversation_id> DAYS=7     # a wider window
make revisions U=<conversation_id>         # the change history, oldest first
make revisions U=<conversation_id> REV=<revision_id>   # one change, both sides untrimmed
```

In prod, Cloud Scheduler POSTs `/curate` nightly at 04:00 America/Los_Angeles
(`infrastructure/services/curator_schedule.py`); an empty body means "every conversation with recent
traffic". Retries are off: a retry would re-apply edits the first attempt already made.

The owner can also run it from the chat: **`/curate`** (`app/chat/curate.py`) runs one pass over that
chat and reports as usual — the same `Curator`, so nothing about the pass differs from the nightly
one. It blocks for minutes while the agent works, which the inbound Cloud Task's 30-minute deadline
covers; nothing serialises a manual pass against the nightly one, so running it at 04:00 would have
two passes editing the same stores.

## Verified behaviour

A seeded day (a correction repeated twice, a standing directive, duplicate memories, a stale fact, a
case-duplicated list, praise, and a 👍) produced, on two consecutive runs:

- the repeated currency correction promoted to core memory, justified by **both** occurrences;
- the duplicate memories merged into one, the loser soft-deleted;
- the stale city corrected in place, keeping the prior value in the record;
- the list de-duplicated;
- praise and the 👍 explicitly named in the report as **not** grounds for any change;
- the one-off trip queries explicitly not stored as facts.

Every one of those changes is in `revisions` with its `before`. The first run exposed a real gap —
the list dedupe ran as clear-then-add and only the clear was recorded, so the history read "list
destroyed"; `ListStore.add` now records too.

## What the owner is told

`/help` closes with the two things they cannot discover from the command list: that a reaction is the
cheapest way to signal (and that words beat an emoji when something is wrong), and that a nightly
pass edits these stores and reports in the morning. The report itself arrives as a Telegram message,
split to the size limit like every other send.

## Known limits

- **Emoji have no fixed meaning.** The curator reads the emoji itself and the conversation around it;
  no polarity table is hardcoded, because what a given emoji means is the owner's call and has not
  been decided (`app/IDEAS.md`, owner wishlist).
- **Sub-agent edits take effect on the next process start** — a conversation's agent is built once
  and cached.
- **No rollback command yet.** `make revisions … REV=<id>` prints the replaced text in full, so the
  undo is a copy-paste rather than the guesswork it was while the listing trimmed at 400 characters —
  but putting a version back is still a manual edit, and nothing the curator itself can do.
