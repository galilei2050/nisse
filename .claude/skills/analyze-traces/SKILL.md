---
name: analyze-traces
description: Pull and analyze the most recent production agent traces for nisse — what the bot was asked, how it reasoned, which tools it used, and whether it under-thought. Use when the user says "разбери трейсы", "analyze last traces", or asks why the assistant gave a weak/shallow answer.
---

# Analyze recent agent traces

Production traces of every `Assistant.run()` land in GCS as gzipped JSON, summaries in Mongo `traces`.
Full trace schema: `baski/agents/trace.py` (`TraceRecord` → `turns[] → TurnRecord`).

- **Bucket:** `gs://nisse2050-private/traces/<uuid>.json.gz`
- **Project:** `nisse2050`

## Steps

1. **List the latest traces** (sorted by upload time):
   ```bash
   gcloud storage ls -l "gs://nisse2050-private/traces/" 2>/dev/null | sort -k2 | tail -25
   ```

2. **Download the last few into `scratch/traces/`** (git-ignored — never the repo root):
   ```bash
   mkdir -p scratch/traces
   gcloud storage cp "gs://nisse2050-private/traces/<id>.json.gz" scratch/traces/ && gunzip -f scratch/traces/<id>.json.gz
   ```

3. **Summarize** with the helper (per-turn thinking size, tool calls, final text, and the completeness-judge
   verdicts — `JUDGE #n PASS/REDO` with `missing`/`feedback`, from `result.judge_verdicts`):
   ```bash
   uv run python .claude/skills/analyze-traces/summarize.py scratch/traces/<id>.json ...
   ```
   To read a full answer verbatim: `show_text.py <file.json>` prints user request, tools, and the final text.

## What "didn't think enough" actually looks like

Judge behavior, not the thinking field — see the caveat below. Real under-thinking signals:

- **No tool grounding when the user asked for data.** User says "основанную на данных" / "посчитай" / "по факту" but the turn has `tools: []` and the answer is invented numbers from memory. The bot *can* call `google_search` / `google_ai_answer` (earlier turns in the same session often do) — dropping it on the data-demanding turn is the tell.
- **One-shot answer to a multi-step ask.** `turns: 1`, no tools, on a "build me a model / compare / verify" request that warranted lookups or staged reasoning.
- **Fabricated specifics disclaimed as assumptions** ("числа — допущения, не факт рынка") when real figures were searchable.
- **Errors / refusals:** `error` non-null, or `stop_reason: refusal`.

## Caveat: traces are BLIND to thinking by default

`thinking` comes back as `['']` (empty) in almost every turn. This is **not** proof the model didn't
reason — Opus 4.7+ adaptive thinking defaults to `display: "omitted"`, so `block.thinking` is an empty
string even when it thought hard (confirmed via Anthropic docs, 2026-06). To actually see reasoning in
future traces, baski must set `thinking: {"type": "adaptive", "display": "summarized"}` in
`baski/agents/agent.py` (params). Until then, diagnose from *tool use + output quality*, not the empty
thinking field. Depth on adaptive is steered by the `effort` setting + prompt, not `budget_tokens`
(which 400s on Opus 4.7+).

## Cleanup

Downloaded traces are throwaway → leave them in `scratch/` (git-ignored). Never `rm`.
