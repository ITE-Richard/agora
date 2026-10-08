"""
工作分配比例：自動挑選接手方時（assign 沒指定 --to、額度不足的交接提醒），讓各 AI 的工作量接近設定的比例。

- 比例：.agora/config.json 的 parties.<AI>.share（非負整數，預設 1；0 表示只參與討論、不自動分派給它）
- 工作量：接手方每跑完一輪記一筆在 .agora/work_log.jsonl，統計最近 SHARE_DAYS 天的輪數；
  進行中的分派先各算 1 輪，避免連續分派都挑到同一方。
- 挑選：在額度足夠、比例 > 0 的候選中，挑「(已做輪數 + 1) / 比例」最小的一方，
  也就是再分一輪給它之後，占比仍最落後的那個；平手時挑比例高的，再平手挑額度多的。
"""

import json
import os
import time
from pathlib import Path
from typing import Dict, Iterable, Optional

from agoralib import quota

SHARE_DAYS = float(os.getenv("AGORA_SHARE_DAYS", "7"))
ACTIVE_RELAY = ("starting", "running", "waiting")


def log_path(root: Path) -> Path:
    return root / ".agora" / "work_log.jsonl"


def record(root: Path, party: str, thread: str):
    log_path(root).parent.mkdir(parents=True, exist_ok=True)
    with open(log_path(root), "a", encoding="utf-8") as f:
        f.write(json.dumps({"time": time.time(), "party": party, "thread": thread}) + "\n")


def recent_work(root: Path, days: float = SHARE_DAYS, include_active: bool = True) -> Dict[str, int]:
    """最近 days 天各 AI 的接手輪數（include_active：進行中的分派各加 1 輪）"""
    since = time.time() - days * 86400
    work: Dict[str, int] = {}
    try:
        for line in log_path(root).read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("time", 0) >= since:
                work[entry["party"]] = work.get(entry["party"], 0) + 1
    except OSError:
        pass
    if include_active:
        for state_file in (root / ".agora" / "threads").glob("*/state.json"):
            try:
                relay = json.loads(state_file.read_text(encoding="utf-8")).get("relay") or {}
            except Exception:
                continue
            if relay.get("status") in ACTIVE_RELAY and relay.get("worker"):
                work[relay["worker"]] = work.get(relay["worker"], 0) + 1
    return work


def pick(candidates: Iterable[str], shares: Dict[str, int], work: Dict[str, int],
         usages: Dict[str, Optional[dict]]) -> Optional[str]:
    """在 candidates 中依比例挑一方；額度不足或比例為 0 的不挑，沒有人可挑時回傳 None"""
    eligible = [p for p in candidates if shares.get(p, 1) > 0 and not quota.low(usages.get(p))]
    if not eligible:
        return None
    return min(eligible, key=lambda p: ((work.get(p, 0) + 1) / shares.get(p, 1), -shares.get(p, 1),
                                        -((usages.get(p) or {}).get("remaining_pct", 0))))
