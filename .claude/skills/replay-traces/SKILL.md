---
name: replay-traces
description: Re-grade recorded nisse traces through the CURRENT completeness judge to measure false-positive / false-negative regression before/after a judge-prompt change. Use when touching baski's GeminiJudge prompt, or when the user says "replay traces", "test the judge", "did the judge change regress".
---

# Replay traces through the judge

The completeness judge (`baski.agents.GeminiJudge`, gemini-flash) grades whether a reply FINISHED
the ask. It mis-reads "research depth" in both directions — see `docs/judge_test_cases.md` for the
root miscalibration and the per-case catalog. This skill re-grades known cases with the **live**
judge prompt so a fix in one direction doesn't silently regress the other.

Judge isolated, no agent run, no Mongo writes — just `GeminiJudge.evaluate(transcript, answer, rules)`
reconstructed from each trace. Flash is nondeterministic, so every case is graded **3×** — read the
distribution, never a single run (the project's empirical rule).

## Two harnesses

1. **`replay.py` — real traces.** Re-grades the FINAL answer of each catalogued production trace and
   compares to its expected verdict (FP/FN/held). Needs the traces downloaded first.
2. **`depth_probe.py` — synthetic.** Grades hand-built SHALLOW / DEEP / INCOMPLETE answers; no traces
   needed. Fast smoke test that the judge still separates shallow-or-incomplete (REDO) from
   deep-and-complete (PASS).

## Run

```bash
# 1. download the catalogued traces (once) — they live gitignored in scratch/traces/
#    use the analyze-traces skill, or:
mkdir -p scratch/traces
for id in bd4e744d a17c09ca c596c18b 06447c3e a36b13b0 f68e23aa 761e9231 b49a0fdf \
          311ec97f cf98e596 1c9b2768 711c3e08 b869e24d 84f43c4b d67d8577; do
  gcloud storage cp "gs://nisse2050-private/traces/$id*.json.gz" scratch/traces/ 2>/dev/null
done
gunzip -f scratch/traces/*.gz

# 2. synthetic smoke test (fast)
uv run --env-file .env python .claude/skills/replay-traces/depth_probe.py

# 3. full catalog (all cases) or a subset by id-prefix
uv run --env-file .env python .claude/skills/replay-traces/replay.py
uv run --env-file .env python .claude/skills/replay-traces/replay.py a17c09ca c596c18b
```

Each line prints `ok`/`!!`, the case, the expected verdict, and the `N/3 PASS` distribution.

## Reading the result

- **FP cases** (`a17c09ca`, `c596c18b`, `06447c3e`) must be **PASS** — they were destructive REDOs.
- **Held PASS / Held REDO→PASS** cases must stay **PASS** on the final answer — no over-firing.
- **`bd4e744d`** (FN) is a **documented borderline** that still PASSes; don't chase it with prompt
  tweaks (overfits + risks FP churn — the fix is agent-side). **`d67d8577`** is **noisy** (complete
  but answered EN-to-RU). Both are expected-imperfect; the rest must be green.

## Adding a case

Append `id-prefix -> (label, "PASS"|"REDO")` to `CATALOG` in `replay.py`, download that trace, and
write it up in `docs/judge_test_cases.md` (request, what the agent did, old vs new verdict, why).
