"""
Claude Code、Antigravity、Codex 剩餘額度查詢

- Claude：快照 ~/.claude/usage_snapshot.json。來源有三個：
  status line（tools/usage/statusline.py）、Agora 呼叫背景 Claude 時的 rate_limit_event、
  以及 refresh 時用 Haiku 發一個極短請求取得的 rate_limit_event。
- Antigravity：仿照 Quota Deck，找出執行中的 agy hub / language server，
  呼叫本機 loopback 的 GetUserStatus 取得各模型 quotaInfo；結果快取在 ~/.agora/antigravity_quota.json。
- Codex：Codex 每次執行都把 rate_limits 寫進 ~/.codex/sessions/**/rollout-*.jsonl，讀最近一筆即可，不需額外請求。

用法（透過 agora.py）：
  python agora.py quota [--refresh] [--json]
"""

import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

if sys.platform.startswith("win"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Antigravity 有多個 hub 時，優先選開著目前工作區的那個
WORKSPACE = Path(os.getenv("AGORA_WORKSPACE") or os.getcwd())
AGORA_HOME = Path.home() / ".agora"
CLAUDE_SNAPSHOT = Path.home() / ".claude" / "usage_snapshot.json"
AGY_CACHE = AGORA_HOME / "antigravity_quota.json"
REFRESH_LOCK = AGORA_HOME / "refresh.lock"
GEMINI_DIR = Path.home() / ".gemini"
LS_SERVICE = "/exa.language_server_pb.LanguageServerService"
PROCESS_NAMES = ("agy.exe", "language_server_windows_x64.exe", "language_server.exe")

THRESHOLD_PCT = float(os.getenv("AGORA_QUOTA_THRESHOLD", "5"))
# Antigravity 內 Claude 系列與 Gemini 系列是獨立額度池；agy 預設用 Gemini
AGY_POOL = os.getenv("AGORA_AGY_POOL", "gemini")
PROBE_ENV = "AGORA_PROBE"

try:
    AGORA_HOME.mkdir(exist_ok=True)
except OSError:  # 沙箱內的接手方可能無權建立
    pass


def _write_json(path: Path, data: dict):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def find_claude() -> str:
    if os.getenv("CLAUDE_PATH"):
        return os.environ["CLAUDE_PATH"]
    target_name = "claude.exe" if sys.platform.startswith("win") else "claude"
    ext_dir = Path.home() / ".vscode" / "extensions"
    candidates = [p for p in ext_dir.glob("anthropic.claude-code-*/resources/native-binary/*") if p.name == target_name]

    def version(p: Path):
        m = re.search(r"claude-code-([\d.]+)", str(p))
        return tuple(int(x) for x in m.group(1).split(".")) if m else ()

    if candidates:
        return str(max(candidates, key=version))
    return shutil.which("claude") or "claude"


# ───────────────────────── Claude ─────────────────────────

def windows_from_rate_limit_info(info: dict) -> dict:
    """stream-json 的 rate_limit_info → 快照格式（utilization 是 0~1 的比例）"""
    windows = {}
    for key, w in (info.get("unifiedWindows") or {}).items():
        if key in ("five_hour", "seven_day") and isinstance(w, dict) and "utilization" in w:
            windows[key] = {"used_percentage": round(w["utilization"] * 100, 1), "resets_at": w.get("resetsAt")}
    if not windows and info.get("rateLimitType") in ("five_hour", "seven_day") and "utilization" in info:
        windows[info["rateLimitType"]] = {
            "used_percentage": round(info["utilization"] * 100, 1), "resets_at": info.get("resetsAt")}
    return windows


def write_claude_snapshot(windows: dict, source: str):
    if windows:
        _write_json(CLAUDE_SNAPSHOT, {"updated_at": int(time.time()), "source": source, **windows})


def probe_claude(timeout: int = 90) -> bool:
    """用 Haiku 發一個不帶工具的極短請求，從 rate_limit_event 取得用量"""
    cmd = [find_claude(), "-p", "ok", "--model", "haiku", "--output-format", "stream-json", "--verbose",
           "--tools", "", "--no-session-persistence", "--settings", '{"disableAllHooks": true}']
    try:
        proc = subprocess.run(cmd, cwd=AGORA_HOME, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, env={**os.environ, PROBE_ENV: "1"})
    except Exception:
        return False
    for line in proc.stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "rate_limit_event":
            write_claude_snapshot(windows_from_rate_limit_info(event.get("rate_limit_info") or {}), "probe")
            return True
    return False


def claude_usage() -> dict | None:
    """回傳最緊的視窗：{remaining_pct, resets_at, window, updated_at}"""
    snap = _read_json(CLAUDE_SNAPSHOT)
    if not snap:
        return None
    now = time.time()
    candidates = []
    for key in ("five_hour", "seven_day"):
        w = snap.get(key)
        if not isinstance(w, dict) or w.get("used_percentage") is None:
            continue
        if w.get("resets_at") and w["resets_at"] < now:
            continue  # 視窗已重置，舊數字作廢
        candidates.append({"window": key, "remaining_pct": round(100 - w["used_percentage"], 1),
                           "resets_at": w.get("resets_at")})
    result = min(candidates, key=lambda c: c["remaining_pct"]) if candidates else \
        {"window": None, "remaining_pct": 100.0, "resets_at": None}
    result["updated_at"] = snap.get("updated_at")
    return result


# ───────────────────────── Codex ─────────────────────────

CODEX_SESSIONS = Path(os.getenv("CODEX_HOME") or Path.home() / ".codex") / "sessions"
CODEX_SNAPSHOT = AGORA_HOME / "codex_quota.json"


def find_codex() -> str:
    if os.getenv("CODEX_PATH"):
        return os.environ["CODEX_PATH"]
    target_name = "codex.exe" if sys.platform.startswith("win") else "codex"
    ext_dir = Path.home() / ".vscode" / "extensions"
    candidates = [p for p in ext_dir.glob("openai.chatgpt-*/bin/*/codex*") if p.name == target_name]

    def version(p: Path):
        m = re.search(r"chatgpt-([\d.]+)", str(p))
        return tuple(int(x) for x in m.group(1).split(".") if x.isdigit()) if m else ()

    if candidates:
        return str(max(candidates, key=version))
    return shutil.which("codex") or "codex"


def _codex_limits_from_file(path: Path) -> dict | None:
    """讀一個 Codex 工作階段檔裡最後一筆 rate_limits"""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return None
    for line in reversed(lines):
        if '"rate_limits"' not in line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        stack = [obj]
        while stack:  # rate_limits 的巢狀位置隨版本不同，遞迴尋找
            cur = stack.pop()
            if isinstance(cur, dict):
                if isinstance(cur.get("rate_limits"), dict):
                    return cur["rate_limits"]
                stack.extend(cur.values())
            elif isinstance(cur, list):
                stack.extend(cur)
    return None


def update_codex_snapshot_from_session(thread_id: str | None = None) -> bool:
    """從指定（或最近一次）的 Codex 工作階段讀出額度並快取"""
    if not CODEX_SESSIONS.exists():
        return False
    pattern = f"*/*/*/rollout-*{thread_id}.jsonl" if thread_id else "*/*/*/rollout-*.jsonl"
    files = sorted(CODEX_SESSIONS.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)[:5]
    for f in files:
        limits = _codex_limits_from_file(f)
        if limits:
            windows = {}
            for key in ("primary", "secondary"):
                w = limits.get(key)
                if isinstance(w, dict) and w.get("used_percent") is not None:
                    windows[key] = {"used_percentage": float(w["used_percent"]), "resets_at": w.get("resets_at"),
                                    "window_minutes": w.get("window_minutes")}
            if windows:
                _write_json(CODEX_SNAPSHOT, {"updated_at": int(f.stat().st_mtime), "plan": limits.get("plan_type"),
                                             **windows})
                return True
    return False


def codex_usage() -> dict | None:
    """回傳最緊的視窗：{remaining_pct, resets_at, window, updated_at}"""
    snap = _read_json(CODEX_SNAPSHOT)
    if not snap and update_codex_snapshot_from_session():
        snap = _read_json(CODEX_SNAPSHOT)
    if not snap:
        return None
    now = time.time()
    candidates = []
    for key in ("primary", "secondary"):
        w = snap.get(key)
        if not isinstance(w, dict):
            continue
        if w.get("resets_at") and w["resets_at"] < now:
            continue
        candidates.append({"window": key, "remaining_pct": round(100 - w["used_percentage"], 1),
                           "resets_at": w.get("resets_at")})
    result = min(candidates, key=lambda c: c["remaining_pct"]) if candidates else         {"window": None, "remaining_pct": 100.0, "resets_at": None}
    result["updated_at"] = snap.get("updated_at")
    return result


# ───────────────────────── Antigravity ─────────────────────────

def _list_processes() -> list[dict]:
    names = " OR ".join(f"Name='{n}'" for n in PROCESS_NAMES)
    cmd = f'Get-CimInstance Win32_Process -Filter "{names}" | Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress'
    try:
        out = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", cmd],
                             capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20).stdout.strip()
        data = json.loads(out) if out else []
    except Exception:
        return []
    return data if isinstance(data, list) else [data]


def _arg(cmdline: str, name: str) -> str | None:
    m = re.search(rf"--?{name}[= ]+\"?([^\s\"]+)", cmdline or "")
    return m.group(1) if m else None


def _candidates() -> list[dict]:
    """(protocol, port, csrf) 候選；開著本專案的 hub 優先，探索檔最後"""
    found, seen = [], set()

    def add(proto, port, csrf, priority):
        if port and (proto, str(port)) not in seen:
            seen.add((proto, str(port)))
            found.append({"protocol": proto, "port": int(port), "csrf": csrf, "priority": priority})

    ws = str(WORKSPACE).lower().replace("/", "\\")
    for p in _list_processes():
        cl = p.get("CommandLine") or ""
        prio = 0 if ws in cl.lower().replace("/", "\\") else 1
        add("http", _arg(cl, "hub-port"), None, prio)
        csrf = _arg(cl, "csrf_token")
        add("http", _arg(cl, "http_server_port"), csrf, prio)
        add("https", _arg(cl, "https_server_port"), csrf, prio)
    for f in GEMINI_DIR.glob("*/daemon/ls_*.json"):
        d = _read_json(f) or {}
        add("http", d.get("httpPort"), d.get("csrfToken"), 2)
        add("https", d.get("httpsPort"), d.get("csrfToken"), 2)
    return sorted(found, key=lambda c: c["priority"])


def _request(c: dict, path: str, body: bytes | None = None, headers: dict | None = None, timeout: float = 4) -> str:
    req = urllib.request.Request(f"{c['protocol']}://127.0.0.1:{c['port']}{path}", data=body,
                                 method="POST" if body is not None else "GET",
                                 headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout, context=ssl._create_unverified_context()) as r:
        return r.read().decode("utf-8", errors="replace")


def _pool_of(label: str) -> str:
    label = (label or "").lower()
    return "claude" if "claude" in label else "gemini" if "gemini" in label else "other"


def query_antigravity() -> dict | None:
    """即時查詢；找不到執行中的 Antigravity 時回傳 None"""
    for c in _candidates():
        try:
            csrf = c["csrf"]
            if not csrf:
                m = re.search(r'"csrfToken"\s*:\s*"([^"]+)"', _request(c, "/"))
                csrf = m.group(1) if m else None
            if not csrf:
                continue
            status = json.loads(_request(c, f"{LS_SERVICE}/GetUserStatus", b"{}", {"x-codeium-csrf-token": csrf}))
        except Exception:
            continue
        configs = ((status.get("userStatus") or {}).get("cascadeModelConfigData") or {}).get("clientModelConfigs") or []
        pools = {}
        for m in configs:
            q = m.get("quotaInfo") or {}
            if q.get("remainingFraction") is None and not q.get("resetTime"):
                continue
            try:
                reset = int(datetime.fromisoformat(q["resetTime"].replace("Z", "+00:00")).timestamp()) if q.get("resetTime") else None
            except ValueError:
                reset = None
            entry = {"label": m.get("label"), "remaining_pct": round(float(q.get("remainingFraction") or 0) * 100, 1),
                     "resets_at": reset}
            pool = _pool_of(entry["label"])
            if pool not in pools or entry["remaining_pct"] < pools[pool]["remaining_pct"]:
                pools[pool] = entry
        if pools:
            return {"updated_at": int(time.time()), "pools": pools}
    return None


def antigravity_usage() -> dict | None:
    """讀快取，回傳 agy 預設額度池：{remaining_pct, resets_at, label, pools, updated_at}"""
    cache = _read_json(AGY_CACHE)
    if not cache or not cache.get("pools"):
        return None
    pool = cache["pools"].get(AGY_POOL) or min(cache["pools"].values(), key=lambda p: p["remaining_pct"])
    if pool.get("resets_at") and pool["resets_at"] < time.time():
        pool = {**pool, "remaining_pct": 100.0}  # 已過重置時間
    return {**pool, "pools": cache["pools"], "updated_at": cache["updated_at"]}


# ───────────────────────── 更新與摘要 ─────────────────────────

def refresh(force: bool = False, min_interval: int = 60) -> None:
    """重新查詢兩方並寫入快取；多個呼叫者同時觸發時只跑一次"""
    if os.getenv("AGORA_INVOKED"):
        return  # 接手方常在沙箱裡（如 Codex workspace-write），寫不到 ~/.agora；只讀快取，由主程序負責更新
    if not force and REFRESH_LOCK.exists() and time.time() - REFRESH_LOCK.stat().st_mtime < min_interval:
        return
    try:
        REFRESH_LOCK.write_text(str(os.getpid()))
    except OSError:
        return
    agy = query_antigravity()
    if agy:
        _write_json(AGY_CACHE, agy)
    update_codex_snapshot_from_session()
    probe_claude()


def is_stale(usage: dict | None, max_age: int) -> bool:
    return not usage or time.time() - (usage.get("updated_at") or 0) > max_age


def wanted_max_age(*usages) -> int:
    """額度越低更新越頻繁"""
    low = min((u["remaining_pct"] for u in usages if u), default=100)
    return 180 if low < 20 else 600


def refresh_in_background():
    """丟到背景執行 refresh，不阻塞呼叫者（hook 用）"""
    if REFRESH_LOCK.exists() and time.time() - REFRESH_LOCK.stat().st_mtime < 120:
        return
    kwargs = {}
    if sys.platform.startswith("win"):
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        kwargs["creationflags"] = flags
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--refresh", "--quiet"],
                     cwd=AGORA_HOME, close_fds=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, **kwargs)


def fmt_time(ts) -> str:
    if not ts:
        return "未知"
    dt = datetime.fromtimestamp(ts)
    return dt.strftime("%H:%M") if dt.date() == datetime.now().date() else dt.strftime("%m/%d %H:%M")


def describe(name: str, usage: dict | None) -> str:
    if not usage:
        return f"{name}：無資料"
    age = int((time.time() - (usage.get("updated_at") or 0)) / 60)
    return f"{name} 剩 {usage['remaining_pct']:.0f}%（{fmt_time(usage.get('resets_at'))} 重置，{age} 分鐘前）"


def low(usage: dict | None) -> bool:
    return bool(usage) and usage["remaining_pct"] < THRESHOLD_PCT


USAGE = {"claude": claude_usage, "antigravity": antigravity_usage, "codex": codex_usage}
LABELS = {"claude": "Claude", "antigravity": "Antigravity", "codex": "Codex"}


def usage(party: str) -> dict | None:
    return USAGE[party]()


def all_usage() -> dict:
    return {p: f() for p, f in USAGE.items()}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if "--refresh" in argv:
        refresh(force=True)
        if "--quiet" in argv:
            return
    usages = all_usage()
    if "--refresh" not in argv and any(is_stale(usages[p], 600) for p in ("claude", "antigravity")):
        refresh(force=True)
        usages = all_usage()
    if "--json" in argv:
        print(json.dumps({**usages, "threshold_pct": THRESHOLD_PCT}, ensure_ascii=False, indent=2))
        return
    for party, u in usages.items():
        label = f"Antigravity（{AGY_POOL}）" if party == "antigravity" else LABELS[party]
        print(describe(label, u))
        if party == "antigravity" and u:
            others = "、".join(f"{k} {v['remaining_pct']:.0f}%" for k, v in u["pools"].items() if k != AGY_POOL)
            if others:
                print(f"  其他額度池：{others}")
    for party, u in usages.items():
        if low(u):
            print(f"⚠️ {LABELS[party]} 低於 {THRESHOLD_PCT:.0f}%")


if __name__ == "__main__":
    main()
