# Sub-agents — expectation-first test cases

Verification for the configurable sub-agents feature. Write the expectation, then run; a case fails
if the observed trace doesn't match. Probe harness: `app/CLAUDE.md` → "Manual probe".

## Unit (in `make ci`, real env from `.env` / CI job)

`tests/subagents/test_registry.py`:
- **Unknown tool name → loud raise.** `build_tools(["google_search", "not_a_real_tool"], …)` raises
  `ValueError` naming the bad token. A bad seed config never silently produces a partial toolset.
  Pure guard, no deps. Whether registry keys match real tool `.name`s (a typo'd key) is caught by the
  probe below — build_tools raises at conversation build — not by stubbing real clients in a vacuum.

## End-to-end (real API + Mongo, via `make probe`)

Setup: `uv run --env-file .env python scratch/seed_subagents.py --user-id 777` seeds the `researcher`
(sonnet, 180k context, `google_search`/`google_ai_answer`/`google_news`/`youtube_transcript`/`browse_website`,
compression-demanding system + judge prompts). Use a **fresh** `U=777`.

`make probe U=777 MSG="Исследуй стратегию завести друзей"` — expectations, checked from the trace:

1. **Parent sees the tool.** The injected tool schemas include `researcher` with its description.
2. **Parent delegates.** The parent calls `researcher(prompt=…)`; the `prompt` is a self-contained
   brief (goal/format/boundaries), not a bare echo of the user message.
3. **Child ran isolated & correctly configured.** The child's own trace shows: model `claude-sonnet-4-6`;
   only its five configured leaves available (no memory/list/schedule/subagent tools); several search
   and browse calls; its own judge rubric (not the default) at the loop exit.
4. **Compressed, cited return.** The child's returned string is a bottom line + cited bullets — not a
   raw search dump. Falsifiable bar: no verbatim tool-result blocks; sources named; length ≪ the raw
   material it read (a few hundred words, not thousands).
5. **Parent's final answer** to the owner is sensible and built on the researcher's synthesis.

### Known thing to watch (from review)

- **Does the `judge_prompt` axis actually bite?** In task mode the prompt is *pinned*, not in
  `message_history`, and `format_for_judge` reads only committed history — so the child's judge may
  grade a transcript that lacks the original task. Confirm on the probe trace that the child's judge
  verdict references the task/rubric meaningfully. If it can't see what it's grading, `judge_prompt`
  is decorative and we reconsider it on this evidence — not by guessing.

## Regression

Re-run the parent probe WITHOUT a seeded subagent for a fresh `U=`: the parent has no `researcher`
tool and answers directly — confirms sub-agents are additive and conversation-scoped (never leak
across chats).
