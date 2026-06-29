"""Compact-print agent traces: user request, per-turn thinking/tool/text, result.

Usage: uv run python .claude/skills/analyze-traces/summarize.py scratch/traces/*.json
Note: `thinking` is empty (`['']`) by default — Opus 4.7+ omits it from the response. See SKILL.md.
"""

import json
import sys
from pathlib import Path

for path in sys.argv[1:]:
    t = json.loads(Path(path).read_text())
    print("=" * 100)
    print(f"TRACE {t['id']}  created={t['created_at']}  model={t['model']}")
    print(f"USER: {t['user_request'][:300]!r}")
    print(f"turns={len(t['turns'])}  error={t.get('error')}")
    for turn in t["turns"]:
        think_chars = sum(len(x) for x in (turn.get("thinking") or []))
        tools = [tc["name"] for tc in turn.get("tool_calls", [])]
        txt = turn.get("text_response") or ""
        print(f"  T{turn['turn_number']}: think={think_chars}c tools={tools} stop={turn.get('stop_reason')} txt={len(txt)}c")
        for tc in turn.get("tool_calls", []):
            inp = json.dumps(tc.get("input", {}), ensure_ascii=False)
            print(f"     CALL {tc['name']}({inp[:400]})")
        for tr in turn.get("tool_results", []):
            print(f"     RSLT {tr['tool_name']} err={tr['is_error']}: {tr.get('output', '')[:200]!r}")
        if txt:
            print(f"     TEXT: {txt[:500]}")
