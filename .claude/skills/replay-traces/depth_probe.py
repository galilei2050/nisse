"""Synthetic discrimination probe for the completeness judge — no traces needed.

Grades three hand-built answers to investigative asks with the CURRENT judge, N times, and checks
the judge still separates them. Catches a judge-prompt change that either dulls the redo (SHALLOW /
INCOMPLETE start passing) or over-fires (DEEP starts failing). Run from the repo root:

    uv run --env-file .env python .claude/skills/replay-traces/depth_probe.py
"""
import asyncio

from baski.agents import GeminiJudge
from baski.env import get_env

RULES = "Owner: Vova, runs an auto-repair shop. Act, don't ask. Ground analysis in real data; cite sources."
ASK = "[user] Подумай какие есть способы чтобы достать кандидатов в механики"
PLAN_ASK = "[user] Спланируй романтические выходные у океана, бюджет $800: план по часам, места, цены, ссылки."

# name -> (expected, transcript, answer)
CASES = {
    "SHALLOW (generic, 1 search, no sources)": (
        "REDO",
        f'{ASK}\n[tool] google_search({{"query": "how to hire mechanics"}})\n[assistant] ...',
        "Способы: 1) объявления на job-бордах 2) рекомендации знакомых 3) кадровые агентства "
        "4) соцсети 5) хорошая зарплата. Удачи!",
    ),
    "DEEP (targeted searches + browse, figures+sources)": (
        "PASS",
        f'{ASK}\n[tool] google_search({{"query": "recruit auto mechanics sourcing channels 2026"}})\n'
        f'[tool] browse_website({{"url": "https://skylinecollege.edu/automotive"}})\n[assistant] ...',
        "Каналы по экономике buy-vs-build:\n- Skyline College (15 мин): 91% удержание за 5 лет, 100% "
        "трудоустройство утренней программы (skylinecollege.edu); зайти заранее — donate инструмент.\n"
        "- WrenchWay reverse posts: техники постят сами, vs Indeed где их нет.\n- Рефералы: +40% удержание "
        "через год.\nСвязка с твоим retention 55 дней: build-каналы окупаются только если техник остаётся "
        "дольше амортизации.\nИсточники: skylinecollege.edu, wrenchway.com, ASE Connects.",
    ),
    "INCOMPLETE (explicit deliverables missing)": (
        "REDO",
        f'{PLAN_ASK}\n[tool] google_search({{"query": "romantic weekend near ocean"}})\n[assistant] ...',
        "Отличная идея! Съезди в Half Moon Bay — там красивые виды, хорошие рестораны и можно покататься "
        "верхом. Уверен, вам понравится. Хочешь, подберу конкретный отель?",
    ),
}


async def grade(judge, transcript, answer):
    v = await judge.evaluate(transcript=transcript, answer=answer, rules=RULES)
    return "PASS" if v.finished else "REDO"


async def main():
    judge = GeminiJudge(project=str(get_env("GOOGLE_CLOUD_PROJECT")))
    for name, (expect, tr, ans) in CASES.items():
        res = await asyncio.gather(*[grade(judge, tr, ans) for _ in range(3)])
        verdict = "PASS" if res.count("PASS") >= 2 else "REDO"
        mark = "ok " if verdict == expect else "!! "
        print(f"{mark}{name:52} exp {expect:5} {res}")


asyncio.run(main())
