"""Print a trace's user request, tools used, and the FULL final answer text verbatim.

Usage: uv run python .claude/skills/analyze-traces/show_text.py scratch/traces/<id>.json ...
"""

import json
import sys
from pathlib import Path

for path in sys.argv[1:]:
    t = json.loads(Path(path).read_text())
    tools = [tc["name"] for turn in t["turns"] for tc in turn.get("tool_calls", [])]
    print("#" * 80)
    print("USER:", t["user_request"])
    print("TURNS:", len(t["turns"]), "TOOLS:", tools)
    print(t["turns"][-1].get("text_response") or "")
