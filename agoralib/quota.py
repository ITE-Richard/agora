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

from __future__ import annotations   # 支援 Python 3.9（型別標註用到 X | None）

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

AGORA_HOME = Path.home() / ".agora"
CLAUDE_SNAPSHOT = Path.home() / ".claude" / "usage_snapshot.json"
AGY_CACHE = AGORA_HOME / "antigravity_quota.json"
REFRESH_LOCK = AGORA_HOME / "refresh.lock"
GEMINI_DIR = Path.home() / ".gemini"
LS_SERVICE = "/exa.language_server_pb.LanguageServerService"
PROCESS_NAMES = ("agy.exe", "language_server_windows_x64.exe", "language_server.exe")

THRESHOLD_PCT = float(os.getenv("AGORA_QUOTA_THRESHOLD", "5"))
STALE_AFTER = int(os.getenv("AGORA_QUOTA_STALE", "7200"))   # 額度資料超過這麼久沒更新就標示可能過期（秒）
BLOCKED_FILE = AGORA_HOME / "blocked.json"   # 呼叫失敗判定為帳務或權限問題的一方：{party: {reason, message, at}}
# Antigravity 內 Claude 系列與 Gemini 系列是獨立額度池；agy 預設用 Gemini
AGY_POOL = os.getenv("AGORA_AGY_POOL", "gemini")
PROBE_ENV = "AGORA_PROBE"


def _no_window() -> dict:
    """Windows：目前程序沒有主控台時（GetConsoleCP 為 0，例如被 DETACHED_PROCESS 或 GUI 程式啟動），
    主控台子程序會各開一個新視窗（Windows Terminal 會多出一個分頁），所以加 CREATE_NO_WINDOW 給它隱藏主控台；
    有主控台（含隱藏主控台）時子程序直接沿用，在終端機裡執行的行為（例如 Ctrl+C）不變"""
    if not sys.platform.startswith("win"):
        return {}
    try:
        import ctypes
        if ctypes.windll.kernel32.GetConsoleCP():
            return {}
    except (OSError, AttributeError):
        pass
    return {"creationflags": subprocess.CREATE_NO_WINDOW}


NO_WINDOW = _no_window()   # 每個 subprocess 呼叫都帶上：subprocess.run(..., **quota.NO_WINDOW)

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
                              errors="replace", timeout=timeout, env={**os.environ, PROBE_ENV: "1"}, **NO_WINDOW)
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


CLAUDE_WINDOW_LABELS = {
    "five_hour": "5 小時",
    "seven_day": "7 天",
}


def claude_usage() -> dict | None:
    """回傳最緊的視窗：{remaining_pct, resets_at, window, window_label, windows, updated_at}"""
    snap = _read_json(CLAUDE_SNAPSHOT)
    if not snap:
        return None
    now = time.time()
    candidates = []
    windows = {}
    for key in ("five_hour", "seven_day"):
        w = snap.get(key)
        if not isinstance(w, dict) or w.get("used_percentage") is None:
            continue
        label = CLAUDE_WINDOW_LABELS.get(key, key)
        if w.get("resets_at") and w["resets_at"] < now:
            win_data = {"window": key, "label": label, "remaining_pct": 100.0, "resets_at": None}
        else:
            win_data = {"window": key, "label": label, "remaining_pct": round(100 - w["used_percentage"], 1),
                        "resets_at": w.get("resets_at")}
            candidates.append(win_data)
        windows[key] = win_data
    if candidates:
        result = dict(min(candidates, key=lambda c: c["remaining_pct"]))
    else:
        result = {"window": None, "label": None, "remaining_pct": 100.0, "resets_at": None}
    result["window_label"] = result.get("label")
    result["windows"] = windows
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


def codex_window_label(key: str, minutes: int | None) -> str:
    if minutes:
        if minutes == 300:
            return "5 小時"
        if minutes == 10080:
            return "7 天"
        if minutes % 1440 == 0:
            return f"{minutes // 1440} 天"
        if minutes % 60 == 0:
            return f"{minutes // 60} 小時"
        return f"{minutes} 分鐘"
    return "主要" if key == "primary" else "次要"


def codex_usage() -> dict | None:
    """回傳最緊的視窗：{remaining_pct, resets_at, window, window_label, windows, plan, updated_at}"""
    snap = _read_json(CODEX_SNAPSHOT)
    if not snap and update_codex_snapshot_from_session():
        snap = _read_json(CODEX_SNAPSHOT)
    if not snap:
        return None
    now = time.time()
    candidates = []
    windows = {}
    for key in ("primary", "secondary"):
        w = snap.get(key)
        if not isinstance(w, dict) or w.get("used_percentage") is None:
            continue
        minutes = w.get("window_minutes")
        label = codex_window_label(key, minutes)
        if w.get("resets_at") and w["resets_at"] < now:
            win_data = {"window": key, "label": label, "remaining_pct": 100.0, "resets_at": None, "window_minutes": minutes}
        else:
            win_data = {"window": key, "label": label, "remaining_pct": round(100 - w["used_percentage"], 1),
                        "resets_at": w.get("resets_at"), "window_minutes": minutes}
            candidates.append(win_data)
        windows[key] = win_data
    if candidates:
        result = dict(min(candidates, key=lambda c: c["remaining_pct"]))
    else:
        result = {"window": None, "label": None, "remaining_pct": 100.0, "resets_at": None}
    result["window_label"] = result.get("label")
    result["windows"] = windows
    result["plan"] = snap.get("plan")
    result["updated_at"] = snap.get("updated_at")
    return result


# ───────────────────────── Antigravity ─────────────────────────

def _list_processes() -> list[dict]:
    names = " OR ".join(f"Name='{n}'" for n in PROCESS_NAMES)
    cmd = f'Get-CimInstance Win32_Process -Filter "{names}" | Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress'
    try:
        out = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", cmd],
                             capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
                             **NO_WINDOW).stdout.strip()
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

    # Antigravity 有多個 hub 時，優先選開著目前工作區的那個；查詢時才讀（cli 解析 --workspace 後才設定 AGORA_WORKSPACE）
    ws = str(Path(os.getenv("AGORA_WORKSPACE") or os.getcwd())).lower().replace("/", "\\")
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


_EFFORT_SUFFIX_RE = re.compile(r"\s*\((?:Low|Medium|High|Minimal|Max|XHigh|Thinking)\)\s*$", re.I)
_ID_EFFORT_SUFFIX_RE = re.compile(r"-(?:low|medium|high|minimal|max|xhigh|thinking)$", re.I)


def _family_label(label: str) -> str:
    """去掉模型顯示名稱尾端的推理強度括號，例如 'Gemini 4 Argon (High)' -> 'Gemini 4 Argon'"""
    return _EFFORT_SUFFIX_RE.sub("", label or "").strip()


def _short_pool_label(family: str) -> str:
    """窄進度條與摘要用的簡短名稱：例如 'Gemini 4 Argon' -> 'Argon'、'Gemini 3.8 Flash' -> 'Flash 3.8'"""
    m = re.match(r"^Gemini\s+(\d+(?:\.\d+)?)\s+(.+)$", family or "", re.I)
    if m:
        ver, name = m.group(1), m.group(2).strip()
        return f"{name} {ver}" if "." in ver else name
    return re.sub(r"^Gemini\s+", "", family or "", flags=re.I).strip() or family


def _pool_of(label: str, model_id: str = "") -> str:
    """模型所屬的額度池代號：
    - Claude 系列（Opus / Sonnet）共用 'claude'
    - Gemini 各系列（如 gemini-4-argon、gemini-3.8-flash）各自獨立成池
    - 其他模型依去強度後的代號分池（無代號時歸入 'other'）"""
    low = f"{label or ''} {model_id or ''}".lower()
    if "claude" in low:
        return "claude"
    base_id = _ID_EFFORT_SUFFIX_RE.sub("", (model_id or "").strip().lower())
    if base_id:
        return base_id
    fam = _family_label(label).lower()
    slug = re.sub(r"[^a-z0-9.]+", "-", fam).strip("-")
    if "gemini" in low:
        return slug or "gemini"
    return slug or "other"


def _match_agy_pool(pools: dict, target: str | None) -> str | None:
    """把設定的模型名稱或額度池名稱對應到 pools 的鍵值"""
    if not target or not pools:
        return None
    t = target.strip().lower()
    if t in pools:
        return t
    pk = _pool_of(t, t)
    if pk in pools:
        return pk
    if t != "gemini":
        for k, v in pools.items():
            if t in k.lower() or t in (v.get("label") or "").lower() or t in (v.get("short_label") or "").lower():
                return k
    return "gemini" if "gemini" in pools else None


def _configured_agy_model() -> str | None:
    """目前工作區設定的 Antigravity 模型（沒設定時回傳 None）"""
    try:
        from agoralib import config
        start = Path(os.getenv("AGORA_WORKSPACE") or os.getcwd()).resolve()
        root = config.find_root(start)
        if root:
            return (config.party_settings(root).get("antigravity") or {}).get("model")
    except Exception:
        pass
    return None


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
        cmcd = (status.get("userStatus") or {}).get("cascadeModelConfigData") or {}
        configs = cmcd.get("clientModelConfigs") or []
        sorts = cmcd.get("clientModelSorts") or []
        order_labels = (((sorts[0].get("groups") or [{}])[0].get("modelLabels")) or []) if sorts else []
        order_index = {lbl: i for i, lbl in enumerate(order_labels)}
        ordered = sorted(configs, key=lambda m: order_index.get(m.get("label"), 999))
        default_alias = ((cmcd.get("defaultOverrideModelConfig") or {}).get("modelOrAlias") or {}).get("model")
        default_model = None
        if default_alias:
            default_model = next((m.get("modelId") or m.get("label") for m in ordered
                                  if (m.get("modelOrAlias") or {}).get("model") == default_alias), None)
        if not default_model and ordered:
            default_model = ordered[0].get("modelId") or ordered[0].get("label")
        pools = {}
        for m in ordered:
            q = m.get("quotaInfo") or {}
            if q.get("remainingFraction") is None and not q.get("resetTime"):
                continue
            try:
                reset = int(datetime.fromisoformat(q["resetTime"].replace("Z", "+00:00")).timestamp()) if q.get("resetTime") else None
            except ValueError:
                reset = None
            pool = _pool_of(m.get("label") or "", m.get("modelId") or "")
            fam = "Claude" if pool == "claude" else (_family_label(m.get("label") or "") or pool)
            entry = {"label": fam, "short_label": _short_pool_label(fam),
                     "remaining_pct": round(float(q.get("remainingFraction") or 0) * 100, 1),
                     "resets_at": reset}
            if pool not in pools or entry["remaining_pct"] < pools[pool]["remaining_pct"]:
                pools[pool] = entry
        if pools:
            return {"schema": 2, "updated_at": int(time.time()), "default_model": default_model, "pools": pools}
    return None


def antigravity_usage(model: str | None = None) -> dict | None:
    """讀快取，依目前專案選用的模型（或 CLI 預設模型）回傳對應額度池，並附上各 Gemini 系列視窗明細"""
    cache = _read_json(AGY_CACHE)
    if not cache or not cache.get("pools"):
        return None
    now = time.time()
    pools = {}
    for k, p in cache["pools"].items():
        if not isinstance(p, dict):
            continue
        fam = "Claude" if k == "claude" else (_family_label(p.get("label") or "") or k)
        entry = {**p, "label": fam, "short_label": p.get("short_label") or _short_pool_label(fam)}
        if entry.get("resets_at") and entry["resets_at"] < now:
            entry = {**entry, "remaining_pct": 100.0, "resets_at": None}
        pools[k] = entry
    if not pools:
        return None
    cfg_model = model or _configured_agy_model()
    env_pool = os.getenv("AGORA_AGY_POOL")
    pool_key = (_match_agy_pool(pools, cfg_model)
                or (_match_agy_pool(pools, env_pool) if env_pool else None)
                or _match_agy_pool(pools, cache.get("default_model"))
                or _match_agy_pool(pools, AGY_POOL))
    if not pool_key:
        gemini_keys = [k for k in pools if "gemini" in k]
        pool_key = min(gemini_keys or list(pools), key=lambda k: pools[k]["remaining_pct"])
    pool = pools[pool_key]
    win_keys = [k for k in pools if "gemini" in k]
    if pool_key not in win_keys:
        win_keys.insert(0, pool_key)
    windows = {
        k: {
            "window": k,
            "label": pools[k]["short_label"],
            "full_label": pools[k]["label"],
            "remaining_pct": pools[k]["remaining_pct"],
            "resets_at": pools[k].get("resets_at"),
            "active": k == pool_key,
        }
        for k in win_keys
    }
    return {
        **pool,
        "window": pool_key,
        "window_label": pool["short_label"],
        "windows": windows,
        "pools": pools,
        "default_model": cache.get("default_model"),
        "configured_model": cfg_model,
        "schema": cache.get("schema"),
        "updated_at": cache.get("updated_at"),
    }


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
    if not usage or time.time() - (usage.get("updated_at") or 0) > max_age:
        return True
    return "pools" in usage and usage.get("schema") != 2


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
        # 隱藏主控台，refresh 呼叫的 CLI 才不會各開一個視窗（見 jobs.FLAGS）
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
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


def age_text(updated_at) -> str:
    """「N 分鐘前」；超過 STALE_AFTER 改用小時並標示可能過期"""
    seconds = time.time() - (updated_at or 0)
    minutes = int(seconds / 60)
    text = f"{minutes} 分鐘前" if minutes < 120 else f"{minutes // 60} 小時前"
    return text + ("，可能過期" if seconds > STALE_AFTER else "")


def describe(name: str, usage: dict | None) -> str:
    if usage and usage.get("blocked"):
        b = usage["blocked"]
        return f"{name}：無法使用（{b.get('reason')}，{fmt_time(b.get('at'))} 偵測）"
    if not usage:
        return f"{name}：無資料"
    win_tag = f"{usage['window_label']}，" if usage.get("window_label") and usage.get("window") else ""
    extra = ""
    if usage.get("pools") and usage.get("windows"):
        others = [f"{w['label']} 剩 {w['remaining_pct']:.0f}%（{fmt_time(w.get('resets_at'))} 重置）"
                  for k, w in usage["windows"].items() if k != usage.get("window")]
        if others:
            extra = "；" + "、".join(others)
    return (f"{name} 剩 {usage['remaining_pct']:.0f}%（{win_tag}{fmt_time(usage.get('resets_at'))} 重置{extra}，"
            f"{age_text(usage.get('updated_at'))}）")


def low(usage: dict | None) -> bool:
    """速率或週期額度低於門檻（等重置就會恢復）"""
    return bool(usage) and usage["remaining_pct"] < THRESHOLD_PCT


def unavailable(usage: dict | None) -> bool:
    """不該分派給它：額度低於門檻，或被標記為帳務／權限問題（blocked）"""
    return low(usage) or bool(usage and usage.get("blocked"))


# ───────────────────────── 帳務或權限問題（blocked） ─────────────────────────

def billing_reason(message: str) -> str:
    """帳務或權限錯誤的簡短說明（顯示用）"""
    m = (message or "").lower()
    if "credit" in m:
        return "workspace 沒有 credits" if "workspace" in m else "credits 已用完"
    if "insufficient_quota" in m:
        return "帳戶額度不足（insufficient_quota）"
    if "billing" in m or "payment" in m:
        return "帳務或付款問題"
    if "subscription" in m:
        return "訂閱問題"
    return "帳務或權限問題"


def blocked_all() -> dict:
    return _read_json(BLOCKED_FILE) or {}


def blocked(party: str) -> dict | None:
    return blocked_all().get(party)


def mark_blocked(party: str, message: str) -> dict:
    """記下這一方暫時不能用（直到下一次成功呼叫，或 agora quota --unblock）"""
    data = blocked_all()
    data[party] = {"reason": billing_reason(message), "message": (message or "")[:500], "at": int(time.time())}
    try:
        _write_json(BLOCKED_FILE, data)
    except OSError:
        pass
    return data[party]


def clear_blocked(party: str) -> bool:
    data = blocked_all()
    if party not in data:
        return False
    data.pop(party)
    try:
        _write_json(BLOCKED_FILE, data)
    except OSError:
        return False
    return True


USAGE = {"claude": claude_usage, "antigravity": antigravity_usage, "codex": codex_usage}
LABELS = {"claude": "Claude", "antigravity": "Antigravity", "codex": "Codex"}


def usage(party: str) -> dict | None:
    """額度；被標記為帳務／權限問題時附上 blocked（額度數字仍照原來的來源，但這一方實際上不能用）"""
    result = USAGE[party]()
    mark = blocked(party)
    if mark:
        result = {**(result or {"remaining_pct": 0.0, "resets_at": None, "window": None, "window_label": None,
                                "updated_at": mark.get("at")}), "blocked": mark}
    return result


def all_usage() -> dict:
    return {p: usage(p) for p in USAGE}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if "--unblock" in argv:
        party = argv[argv.index("--unblock") + 1] if argv.index("--unblock") + 1 < len(argv) else ""
        if party not in USAGE:
            sys.exit(f"--unblock 要指定 {'、'.join(USAGE)} 其中之一。")
        print(f"已清除 {LABELS[party]} 的無法使用標記。" if clear_blocked(party) else f"{LABELS[party]} 沒有無法使用標記。")
        return
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
        print(describe(LABELS[party], u))
        if u and u.get("windows") and len(u["windows"]) > 1:
            details = "、".join(f"{w.get('full_label') or w['label']} 剩 {w['remaining_pct']:.0f}%（{fmt_time(w.get('resets_at'))} 重置）"
                               for w in u["windows"].values())
            print(f"  各視窗明細：{details}")
        if party == "antigravity" and u and u.get("pools"):
            shown = set(u.get("windows") or {}) | {u.get("window")}
            others = "、".join(f"{v.get('label') or k} {v['remaining_pct']:.0f}%"
                               for k, v in u["pools"].items() if k not in shown)
            if others:
                print(f"  其他額度池：{others}")
    for party, u in usages.items():
        if u and u.get("blocked"):
            print(f"⛔ {LABELS[party]} 呼叫失敗需要人工處理：{u['blocked'].get('message') or u['blocked'].get('reason')}")
            print(f"  處理後下一次成功呼叫會自動清除，或執行 agora quota --unblock {party}")
        elif low(u):
            print(f"⚠️ {LABELS[party]} 低於 {THRESHOLD_PCT:.0f}%")


if __name__ == "__main__":
    main()
