# curator/ — the nightly self-maintenance pass

One agent run per night, off the request path, that edits what the assistant knows: core memory,
memories, lists, sub-agents. Design, research grounding, and verified behaviour: `docs/curator.md`.

## Shape

- `evidence.py` — `EvidenceCollector.collect()` folds raw turns into **exchanges** (one owner message
  + the final answer + the reactions on it); `Evidence.render()` makes the digest. A turn is one API call, so a
  question that took three tool rounds spans three turns; ungrouped, the day reads as the assistant
  talking to itself. Reactions resolve via `turn_id` (written by the chat layer at send time), and
  the LAST reaction record for a turn is its current state. A `[Запланировано]` prompt is flagged
  `scheduled` — the bot prompting itself is not owner input.
- `classify.py` — `MessageClassifier.classify()`: one call labelling each owner message
  (`MessageKind`); `Classification.render()` is what the curator's brief quotes. Taxonomy from
  Don-Yehiya et al. / arXiv:2507.23158, not invented. **Offline only** — an inline classifier is a
  rejected direction here. Fails loud on unusable JSON: a silently-empty classification is
  indistinguishable from a quiet night.
- `prompt.py` — `NISSE_CURATOR_PROMPT` (what to look for, the evidence rules, the "do NOT capture"
  list) + `CURATOR_JUDGE_PROMPT` + `REVIEW_BRIEF`. **This file is the feature**; the rest is
  plumbing that delivers evidence and records what changed.
- `curator.py` — `Curator.curate()`: collect → classify → run the agent inside
  `acting_as(CURATOR, run_id=…)` → count the revisions → record the run → message the owner. The
  report is rendered by an injected `format_report` (`chat.format.compose_answer`, supplied in
  `backend.py`) so it ends with the judge's verdict and the cost like any other reply; a domain
  module importing the chat layer would cycle, since `app.chat` drives `/curate`.
  `Curator.ensure_indexes` covers `curator_runs` only: `revisions` is written by every actor, so its
  index is created at startup in `backend.py`.
- `store.py` — `CuratorRun` + `CuratorRunStore` (`curator_runs`): why the night's work happened and
  what it told the owner. An idle pass is recorded too — "ran and found nothing" must not look like
  "never ran".
- `router.py` — `POST /curate`. Empty body (what Cloud Scheduler sends) = every active conversation;
  `{"conversation_id": N}` = one chat. The owner's on-demand entry is the `/curate` command
  (`app/chat/curate.py`), which drives the same `Curator` object over one chat.

## Design facts

- **Same tools as the live assistant**, built from the shared registry (`CURATOR_TOOLS`): one write
  path per store, not a parallel curator-only one that could drift. No web tools, no `ask_user` —
  the owner is asleep. `judge_rules` and `subagents` are curator-only (off `MAIN_TOOLS`).
- **It edits the assistant's judge, not its own.** `update_judge_rules` appends to the rubric the
  assistant's replies are graded by — the lever for a rule the answering model keeps reading and
  ignoring, where another wording of the same core-memory line changes nothing. Its OWN rubric
  (`CURATOR_JUDGE_PROMPT`) stays in code: an agent that can relax its own grader has no grader.
- **Its own judge**, never the assistant's. The completeness rubric grades how fully an answer served
  a request and would push a maintenance pass toward more edits on thinner evidence.
- **Opus, not a cheap model.** It runs once a night on the records that shape every future reply; a
  bad edit here is the "unverifiable miss" the project principles weigh heaviest. The classification
  pass is Sonnet — that one is labelling, not judgement.
- **Attribution is ambient, not a parameter** (`app/shared/revisions.py`). A store is built the same
  way for the assistant and for the curator, so threading an actor through every tool factory would
  be plumbing for one nightly caller.
- **Praise changes nothing.** Grounded in the paper above: positive feedback correlates with slightly
  *worse* prompts. The prompt and the judge rubric both enforce it.

## Testing

`make curate U=<id> [DAYS=n] [DRY=1]` (`app/curate_probe.py`) runs the real pass and prints evidence,
every revision with its `before`, and the report. `make revisions U=<id>` reads the history back.
Use a throwaway conversation id — it edits live stores (recoverably, but they are the owner's).
Unit tests: `tests/curator/` (exchange grouping, retracted reactions, attribution, the sub-agent
write guards, classifier parsing).
