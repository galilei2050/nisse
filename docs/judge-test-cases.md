# Completeness-judge test cases

Expectation-first scenarios for the LLM-as-judge (`baski.agents.GeminiJudge`) that grades whether the
agent FINISHED a turn. Run each with `make probe U=<fresh> MSG="…"`, then read the verdict via
`.claude/skills/analyze-traces/summarize.py <trace>` (`JUDGE #n PASS/REDO`). Verdicts live in
`result.judge_verdicts` of the trace.

Quadrants (positive = judge flags "not finished" / REDO):
- **TN** answer complete (or no task) → PASS. **FP** complete → REDO (over-strict, causes near-duplicate).
- **TP** genuinely incomplete → REDO. **FN** incomplete → PASS (miss).

## Cases & expected verdict

| # | MSG | Expected | Why |
|---|-----|----------|-----|
| 1 | `Пиздато` (reaction, after a prior task) | **PASS** | Non-task; a brief reply is complete. Regression guard for the FP bug where reactions got kicked 3×. |
| 2 | `Здарова! Как сам?` | **PASS** | Greeting / non-task. |
| 3 | `Хочу пассивный доход. С чего начать?` | **PASS** | Open advice request; a substantive answer is complete. |
| 4 | `Сколько спутников у Юпитера? Назови 4 крупнейших.` | **PASS** | Grounded factual; must NOT be kicked as "from the future" (judge has current time + sees `[tool]` lines). |
| 5 | `Сравни в таблице iPhone 16 и Galaxy S25: цена в рублях, экран, батарея, камера, чип. С источниками.` | **PASS** | Multi-part deliverable fully assembled (table + prices + sources). |
| 6 | `Переведи на английский 'Москва не сразу строилась', объясни смысл и приведи английскую пословицу-аналог.` | **PASS** | All 3 sub-parts present → no false-negative. Drop one part and it should flip to REDO. |
| 7 | A complete answer that ends with a trailing offer ("хочешь, могу отслеживать…") | **PASS** | Deliverable is done; a trailing courtesy is NOT a punt — do not redo to strip it (avoids the near-duplicate). |
| 8 | A real task where the agent withholds the work to ask permission first ("сделать тебе X?") without doing it | **REDO** | True punt — work requested, not delivered. |

## What was verified (2026-06-28, model gemini-3.5-flash)

Cases 1–7 ran green (7/7). Trace IDs: 1 `b0ecd87e`, 2 `d6114878`, 3 `6d0f010f`, 4 `761e9231`,
5 `ea23fc19`, 6 `ecf449ae`, 7 (training-plan variant) `6208b425`. The earlier news probe `06447c3e`
showed the case-7 over-strictness (REDO to strip a trailing offer) that the punt-criterion carve-out fixes.

**Gap:** a clean true-positive (case 8) and false-negative are hard to elicit through `make probe`
because the agent reliably produces complete answers; the judge's REDO path is exercised only when its
first tool-free draft is genuinely short. Re-test case 8 if the punt criterion changes.
