"""
清理不再需要的討論串，以及 Agora 為它們在各 CLI 開的對話紀錄。

一個討論串要同時符合下列條件才算「不再需要」：
- 超過 days 天沒有任何活動（討論串資料夾內所有檔案的最後修改時間）
- 沒有標記保留（agora keep）
- 沒有分派工作，或分派的工作已完成（暫停、失敗、等待中、執行中的都保留）
- 目前沒有人在使用（沒有 .lock）
明確指定討論串時不看天數，其餘條件照樣檢查。

對話紀錄只刪 Agora 自己開的（state.json 的 sessions；--session 帶進來的外部對話不刪）：
- Codex：官方指令 codex delete --force <id>
- Claude Code：~/.claude/projects/*/<id>.jsonl 與同名資料夾
- Antigravity：~/.gemini/antigravity-cli 下以對話 ID 命名的檔案，以及 conversation_summaries.db 的索引列；
  對話正在使用（presence 鎖定檔刪不掉）時略過
"""

import json
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from agoralib import quota

FINISHED_RELAY = ("done",)
AGY_HOME = Path.home() / ".gemini" / "antigravity-cli"


def last_activity(thread: Path) -> float:
    return max((p.stat().st_mtime for p in thread.rglob("*") if p.is_file()), default=thread.stat().st_mtime)


def sessions_of(state: dict) -> List[Tuple[str, str]]:
    """Agora 為這個討論串開的對話 (AI, 對話 ID)；外部帶進來的不算"""
    found = [(s["party"], s["id"]) for s in state.get("sessions", []) if s.get("id") and not s.get("external")]
    for party, info in state.get("parties", {}).items():   # 舊版討論串沒有 sessions 紀錄
        if info.get("session_id"):
            found.append((party, info["session_id"]))
    relay = state.get("relay") or {}
    if relay.get("session_id") and "external_session" in relay and relay["session_id"] != relay["external_session"]:
        found.append((relay["worker"], relay["session_id"]))
    external = {s["id"] for s in state.get("sessions", []) if s.get("external")} | {relay.get("external_session")}
    found = [(p, i) for p, i in found if i not in external]
    return list(dict.fromkeys(found))


def why_keep(thread: Path, state: dict, days: Optional[float]) -> Optional[str]:
    """回傳必須保留的原因；None 表示可以清理"""
    if state.get("keep"):
        return "已標記保留"
    relay = state.get("relay")
    if relay and relay.get("status") not in FINISHED_RELAY:
        return f"分派的工作尚未完成（{relay.get('status')}）"
    if (thread / ".lock").exists():
        return "正在使用中"
    if days is not None:
        idle = (time.time() - last_activity(thread)) / 86400
        if idle < days:
            return f"{idle:.0f} 天前仍有活動（未滿 {days:g} 天）"
    return None


def scan(threads: List[Path], days: Optional[float]) -> Tuple[List[dict], List[dict]]:
    candidates, kept = [], []
    for thread in threads:
        try:
            state = json.loads((thread / "state.json").read_text(encoding="utf-8"))
        except Exception:
            kept.append({"id": thread.name, "topic": "?", "reason": "state.json 無法讀取"})
            continue
        size = sum(p.stat().st_size for p in thread.rglob("*") if p.is_file())
        info = {"id": thread.name, "topic": state.get("topic", ""), "messages": len(state.get("messages", [])),
                "idle_days": round((time.time() - last_activity(thread)) / 86400, 1), "bytes": size,
                "sessions": [{"party": p, "id": i} for p, i in sessions_of(state)]}
        reason = why_keep(thread, state, days)
        if reason:
            kept.append({**info, "reason": reason})
        else:
            candidates.append(info)
    return candidates, kept


# ───────────────────────── 各 CLI 的對話紀錄 ─────────────────────────

def delete_codex(session_id: str) -> str:
    proc = subprocess.run([quota.find_codex(), "delete", "--force", session_id], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=60, stdin=subprocess.DEVNULL)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout).strip()[-300:] or f"exit {proc.returncode}")
    return "codex delete"


def delete_claude(session_id: str) -> str:
    removed = 0
    for path in (Path.home() / ".claude" / "projects").glob(f"*/{session_id}*"):
        if path.name not in (f"{session_id}.jsonl", session_id):
            continue
        shutil.rmtree(path) if path.is_dir() else path.unlink()
        removed += 1
    return f"{removed} 個檔案" if removed else "已不存在"


def delete_antigravity(session_id: str) -> str:
    lock = AGY_HOME / "presence" / f"{session_id}.lock"
    try:
        lock.unlink(missing_ok=True)
    except PermissionError:
        raise RuntimeError("對話正在使用中")
    removed = 0
    for path in [AGY_HOME / "conversations" / f"{session_id}.db", AGY_HOME / "brain" / session_id,
                 AGY_HOME / "annotations" / f"{session_id}.pbtxt"]:
        if path.exists():
            shutil.rmtree(path) if path.is_dir() else path.unlink()
            removed += 1
    index = AGY_HOME / "conversation_summaries.db"
    if index.exists():
        with sqlite3.connect(index, timeout=10) as db:
            db.execute("DELETE FROM conversation_summaries WHERE conversation_id = ?", (session_id,))
    return f"{removed} 個檔案" if removed else "已不存在"


DELETERS = {"codex": delete_codex, "claude": delete_claude, "antigravity": delete_antigravity}


def delete_thread(thread: Path, info: dict, with_sessions: bool) -> Dict[str, list]:
    done, errors = [], []
    if with_sessions:
        for s in info["sessions"]:
            try:
                done.append(f"{s['party']} {s['id']}：{DELETERS[s['party']](s['id'])}")
            except Exception as e:
                errors.append(f"{s['party']} {s['id']}：{e}")
    shutil.rmtree(thread)
    return {"done": done, "errors": errors}


def compact_work_log(root: Path, keep_days: float):
    """work_log.jsonl 只需要最近 keep_days 天的紀錄"""
    path = root / ".agora" / "work_log.jsonl"
    if not path.exists():
        return
    since = time.time() - keep_days * 86400
    lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            if json.loads(line).get("time", 0) >= since:
                lines.append(line)
        except json.JSONDecodeError:
            continue
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
