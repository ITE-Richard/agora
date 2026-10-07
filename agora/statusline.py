"""
Claude Code status line：顯示用量，並把 rate_limits 寫入快照檔供 hook 與 Agora 讀取。

快照：~/.claude/usage_snapshot.json
{
  "updated_at": 1760000000,
  "session_id": "...",
  "five_hour": {"used_percentage": 42.0, "resets_at": 1760010000},
  "seven_day": {"used_percentage": 10.0, "resets_at": 1760500000}
}
"""

import json
import sys
import time
from datetime import datetime
from pathlib import Path

SNAPSHOT = Path.home() / ".claude" / "usage_snapshot.json"


def main():
    try:
        data = json.loads(sys.stdin.buffer.read().decode("utf-8") or "{}")
    except Exception:
        data = {}

    limits = data.get("rate_limits") or {}
    windows = {k: limits[k] for k in ("five_hour", "seven_day") if isinstance(limits.get(k), dict)}

    if windows:
        snapshot = {"updated_at": int(time.time()), "session_id": data.get("session_id"), **windows}
        tmp = SNAPSHOT.with_suffix(".tmp")
        tmp.write_text(json.dumps(snapshot), encoding="utf-8")
        tmp.replace(SNAPSHOT)

    parts = []
    model = (data.get("model") or {}).get("display_name")
    if model:
        parts.append(model)
    for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
        w = windows.get(key)
        if w:
            reset = datetime.fromtimestamp(w["resets_at"]).strftime("%m/%d %H:%M" if key == "seven_day" else "%H:%M")
            parts.append(f"{label} {w['used_percentage']:.0f}% (重置 {reset})")
    sys.stdout.buffer.write(" | ".join(parts).encode("utf-8"))


if __name__ == "__main__":
    main()
