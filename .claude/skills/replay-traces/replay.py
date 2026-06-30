"""Re-grade recorded traces with the CURRENT completeness judge to measure FP/FN regression.

Grades the FINAL answer (`result.response`) of each catalog trace with the live judge prompt, N
times (flash is nondeterministic — measure the distribution, never one run), and compares to the
expected verdict. Download the traces first with the `analyze-traces` skill (they live, gitignored,
in scratch/traces/). Run from the repo root:

    uv run --env-file .env python .claude/skills/replay-traces/replay.py            # all catalog cases
    uv run --env-file .env python .claude/skills/replay-traces/replay.py a17c09ca   # a subset by id-prefix

Rationale + per-case write-ups: docs/judge_test_cases.md.
"""
import asyncio
import glob
import json
import sys

from baski.agents import GeminiJudge
from baski.env import get_env

REPEATS = 3

# id-prefix -> (label, expected verdict on the FINAL answer: "PASS" or "REDO")
CATALOG = {
    # False negative — shallow advisory that still passes (borderline; documented, agent-side fix)
    "bd4e744d": ("FN mechanics (shallow advisory)", "REDO"),
    # False positives — were destructive REDOs; the recalibration must make these PASS
    "a17c09ca": ("FP 40-day-plan (real news called 'from the future')", "PASS"),
    "c596c18b": ("FP non-obvious earn (grounded called 'ungrounded/2026')", "PASS"),
    "06447c3e": ("FP benzin (redid only to strip a trailing offer)", "PASS"),
    # Held PASS — correct passes that must stay PASS
    "a36b13b0": ("P benzin current-events", "PASS"),
    "f68e23aa": ("P benzin current-events 2", "PASS"),
    "761e9231": ("P jupiter moons (factual)", "PASS"),
    "b49a0fdf": ("P scheduling routine", "PASS"),
    "311ec97f": ("P settlement payout estimate", "PASS"),
    # Held REDO->PASS — final (completed) answer must PASS
    "cf98e596": ("L weekend plan (final complete)", "PASS"),
    "1c9b2768": ("L weekend plan 3 (final complete)", "PASS"),
    "711c3e08": ("L earn concrete (final complete)", "PASS"),
    "b869e24d": ("L earn liquidation (final complete)", "PASS"),
    "84f43c4b": ("L spothero (final complete)", "PASS"),
    # Complete but answered in EN to a RU ask — OLD and NEW judge both REDO (language). Not a regression.
    "d67d8577": ("L weekend plan EN-to-RU → REDO (lang; OLD==NEW)", "REDO"),
}


def transcript_of(trace):
    """Rebuild MessageHistory.format_for_judge from the trace: [role] text + [tool] name(args).

    Strips the `[Completeness check]` retry-prompt turns the OLD run's judge injected — replaying them
    would prime the new judge to REDO ("you failed"), which is a measurement artifact: on a fresh run
    the new judge passes at attempt 1, so those turns never exist. We grade the final answer against the
    work that produced it, not against the old judge's complaints.
    """
    lines = []
    for turn in trace["turns"]:
        for msg in turn.get("messages", []):
            role = msg.get("role")
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for b in content:
                t = b.get("type")
                if t == "text":
                    text = b.get("text", "")
                    if "[Completeness check]" in text:
                        continue
                    lines.append(f"[{role}] {text}")
                elif t == "tool_use":
                    args = json.dumps(b.get("input") or {}, ensure_ascii=False)[:200]
                    lines.append(f"[tool] {b.get('name')}({args})")
    return "\n".join(lines)


async def grade(judge, transcript, answer, rules):
    v = await judge.evaluate(transcript=transcript, answer=answer, rules=rules)
    return "PASS" if v.finished else "REDO"


async def main():
    only = sys.argv[1:]
    judge = GeminiJudge(project=str(get_env("GOOGLE_CLOUD_PROJECT")))
    ok = bad = 0
    print(f"{'case':56} exp   NEW (x{REPEATS})")
    for pref, (label, expect) in CATALOG.items():
        if only and pref not in only:
            continue
        paths = glob.glob(f"scratch/traces/{pref}*.json")
        if not paths:
            print(f"{pref} {label[:48]:48}  -- trace not downloaded (use analyze-traces)")
            continue
        trace = json.load(open(paths[0]))
        rules = trace.get("system_prompt") or ""
        answer = (trace.get("result") or {}).get("response") or ""
        transcript = transcript_of(trace)
        res = await asyncio.gather(*[grade(judge, transcript, answer, rules) for _ in range(REPEATS)])
        npass = res.count("PASS")
        verdict = "PASS" if npass > REPEATS / 2 else "REDO"
        mark = "ok " if verdict == expect else "!! "
        ok += verdict == expect
        bad += verdict != expect
        print(f"{mark}{pref} {label[:48]:48} {expect:5} {npass}/{REPEATS} PASS {res}")
    print(f"\n{ok} as-expected, {bad} off. (FN bd4e744d is a documented borderline; W d67d8577 is noisy.)")


asyncio.run(main())
