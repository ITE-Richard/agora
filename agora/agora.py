"""
Agora - Claude Code ⇄ Antigravity 雙向討論與工作交接

討論：任一方（Claude Code、Antigravity、Richard）在討論串發言並指定由誰回覆；工具呼叫對方的 CLI
（claude -p / agy -p），把對方還沒看過的訊息一次轉過去。
交接：一方額度快用完時，用 relay 把工作交給另一方在背景執行；接手方定期寫進度、檢查是否被叫停、
檢查自己的額度，額度用完會等到重置後再繼續。原本那一方回來後用 recall 收回。

用法：
  python tools/agora/agora.py new "主題"
  python tools/agora/agora.py send <thread> --from claude|antigravity|richard [--to claude|antigravity|none] [--message TEXT | --file PATH]
  python tools/agora/agora.py reply <thread> --party claude|antigravity
  python tools/agora/agora.py auto <thread> --rounds N
  python tools/agora/agora.py relay <thread> --from claude|antigravity [--message TEXT | --file PATH]   # 交接工作
  python tools/agora/agora.py relay-status <thread> [--wait SECONDS]
  python tools/agora/agora.py recall <thread>                                                         # 請接手方在下一個檢查點暫停
  python tools/agora/agora.py list
  python tools/agora/agora.py show <thread> [--last N]

訊息內容未以 --message / --file 指定時讀 stdin。<thread> 可用完整 ID、前綴或 latest。
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

if sys.platform.startswith("win"):
    for stream in (sys.stdout, sys.stderr, sys.stdin):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

sys.path.insert(0, str(Path(__file__).resolve().parent))
import quota  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
DISCUSSIONS_DIR = REPO_ROOT / "discussions"
TIMEOUT = int(os.getenv("AGORA_TIMEOUT", "600"))
WORK_TIMEOUT = int(os.getenv("AGORA_WORK_TIMEOUT", "7200"))
MAX_WORK_ROUNDS = int(os.getenv("AGORA_MAX_WORK_ROUNDS", "12"))
# Windows 命令列上限約 32k 字元；agy 只能從參數收訊息，超過就改成請它讀檔
MAX_ARGV_CHARS = 20000
# 被 Agora 叫起來的 CLI 會帶著這個環境變數，禁止它再呼叫 Agora，避免無限互相呼叫
NESTED_ENV = "AGORA_INVOKED"

AI_PARTIES = ("claude", "antigravity")
NAMES = {"claude": "Claude Code", "antigravity": "Antigravity", "richard": "Richard"}
OTHER = {"claude": "antigravity", "antigravity": "claude"}
QUOTA_ERROR = re.compile(r"quota|exhaust|rate.?limit|usage limit|429|resource_exhausted|credits", re.I)

PREAMBLE = """\
【Agora 討論環境說明】
你是 {me}，正透過 Agora 橋接工具與 {other} 進行技術討論，專案擁有者 Richard 會閱讀完整逐字稿，也可能插話。
工作區：{repo}

討論規則：
1. 這是純討論。除非訊息中明確寫出「Richard 已授權修改」，否則不要建立、修改、刪除任何檔案，也不要執行會改變狀態的指令（git commit、安裝套件、下單等）。
   查證論點請用讀檔與搜尋工具，不要執行終端指令：這個模式下多數指令會被自動拒絕，導致整輪回覆失敗。
2. 不要自行執行 tools/agora/agora.py；你的回覆會由工具自動轉給對方。
3. 用繁體中文、一般技術文風回覆，不使用角色扮演語氣。
4. 直接表達立場：同意就說同意並補充，不同意就說明理由；引用程式碼時標出 檔案:行號。對方的論點要先查證再接受。
5. 回覆盡量控制在 600 字內，結尾用一兩句話總結你目前的立場或待解問題；若認為已達成共識，明確寫出「【已達成共識】」與共識內容。

討論主題：{topic}
"""

WORK_PREAMBLE = """\
【Agora 工作交接】
你是 {me}。{other} 把下面的工作交接給你（可能因為它的額度不足，或依雙方分工指派），請在交接範圍內完成。
Richard 已授權你在交接單列出的範圍內修改檔案。
工作區：{repo}

界線（遇到需要越界的情況就停下來，寫進進度檔等 Richard 決定）：
- 只修改交接單範圍內的檔案，不要刪除範圍外的檔案。
- 不要 git commit / push / reset / checkout / stash，不要安裝套件，不要讀寫 .env 或任何憑證。
- 不要啟動 main.py，不要連線 IBKR 或執行任何下單相關動作。
- tools/agora/ 底下只能執行 `python tools/agora/quota.py`，不要執行 agora.py。

終端指令只能用以下幾個，而且必須一字不差、不能加任何參數（權限是完整比對）：
`python tools/agora/quota.py`（查額度）、`python tools/agora/check.py`（語法檢查＋全部單元測試）、
`git status`、`git diff`、`git log`。列目錄、讀檔、搜尋、寫檔請用內建的檔案工具；
其他指令（包括帶參數的 python -m unittest）都會被自動拒絕，並讓整輪工作中斷。

工作方式：每完成一個小步驟（約 10~20 分鐘的工作量）就做以下三件事：
1. 在 {progress} 末尾追加一段：時間、完成了什麼、改了哪些檔案、下一步是什麼。
2. 讀 {control}；若 "stop_requested" 為 true，代表 {other} 要求你暫停：把交接說明寫進進度檔後結束這一輪回覆，最後一行寫「【已暫停】」。
3. 執行 `python tools/agora/quota.py`；若你（{me}）的額度低於 {threshold:.0f}%，把交接說明寫進進度檔後結束，最後一行寫「【額度不足】」。
全部完成時，在進度檔寫總結，回覆最後一行寫「【工作完成】」。

以下是 {other} 的交接單：
"""

CONTINUE_PROMPT = """\
【Agora 工作交接・繼續】
請先讀 {progress} 與 {control}，從上次停下的地方繼續。界線與工作方式和第一輪相同：
每完成一小步就更新進度檔、檢查 control.json、執行 quota.py；結束時最後一行寫「【工作完成】」「【已暫停】」或「【額度不足】」。
"""


# ───────────────────────── 基本工具 ─────────────────────────

def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def slugify(text: str) -> str:
    slug = re.sub(r"[^\w一-鿿-]+", "-", text.strip()).strip("-")
    return slug[:40] or "thread"


def find_agy() -> str:
    return os.getenv("AGY_PATH", str(Path.home() / ".gemini" / "bin" / "agy.exe"))


def resolve_thread(ref: str) -> Path:
    threads = sorted(p for p in DISCUSSIONS_DIR.glob("*") if (p / "state.json").exists())
    if not threads:
        sys.exit("尚無任何討論串，請先執行 new。")
    if ref == "latest":
        return max(threads, key=lambda p: (p / "state.json").stat().st_mtime)
    matches = [p for p in threads if p.name == ref] or [p for p in threads if p.name.startswith(ref)]
    if len(matches) != 1:
        sys.exit(f"找不到唯一符合「{ref}」的討論串：{[p.name for p in matches] or '無'}")
    return matches[0]


def load_state(thread: Path) -> dict:
    return json.loads((thread / "state.json").read_text(encoding="utf-8"))


def save_state(thread: Path, state: dict):
    tmp = thread / "state.json.tmp"
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(thread / "state.json")


@contextmanager
def thread_lock(thread: Path, wait: int = 30):
    """同一討論串同時只允許一個呼叫，避免雙方同時發起時互相覆寫狀態"""
    lock = thread / ".lock"
    deadline = time.time() + wait
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            break
        except FileExistsError:
            if time.time() - lock.stat().st_mtime > TIMEOUT + 120:
                lock.unlink(missing_ok=True)  # 前一次呼叫異常中止留下的鎖
                continue
            if time.time() > deadline:
                sys.exit("此討論串正在等待另一方回覆，請稍後再試。")
            time.sleep(1)
    try:
        yield
    finally:
        lock.unlink(missing_ok=True)


def git_status() -> str:
    try:
        return subprocess.run(["git", "status", "--porcelain"], cwd=REPO_ROOT, capture_output=True,
                              text=True, encoding="utf-8", timeout=30).stdout
    except Exception:
        return ""


def append_transcript(thread: Path, text: str):
    with open(thread / "transcript.md", "a", encoding="utf-8") as f:
        f.write(text)


def add_message(thread: Path, state: dict, speaker: str, text: str, via_cli: bool = False, meta: str = ""):
    state["messages"].append({"speaker": speaker, "text": text.strip(), "time": now_str(), "via_cli": via_cli})
    header = f"### {NAMES.get(speaker, speaker)} · {now_str()}" + (f"  _({meta})_" if meta else "")
    append_transcript(thread, f"\n{header}\n\n{text.strip()}\n")


def add_system_note(thread: Path, text: str):
    append_transcript(thread, "\n> " + text.replace("\n", "\n> ") + "\n")


def read_message(args) -> str:
    if args.message is not None:
        text = args.message
    elif args.file:
        text = Path(args.file).read_text(encoding="utf-8")
    else:
        text = sys.stdin.read()
    if not text.strip():
        sys.exit("訊息內容是空的。")
    return text


# ───────────────────────── 呼叫兩方 CLI ─────────────────────────

class CallError(RuntimeError):
    def __init__(self, message: str, quota_exhausted: bool = False, denied: bool = False,
                 session_id: str | None = None):
        super().__init__(message)
        self.quota_exhausted = quota_exhausted
        self.denied = denied          # 有工具呼叫被權限拒絕（agy 非互動模式會因此整輪中斷）
        self.session_id = session_id  # 失敗的那段對話，重試時可以接續


def run_cli(cmd: list, stdin_text: str | None, timeout: int) -> tuple[list[dict], str]:
    """執行 CLI，回傳 (stdout 中每一行可解析的 JSON, stderr)"""
    env = {**os.environ, NESTED_ENV: "1"}
    proc = subprocess.run(cmd, cwd=REPO_ROOT, input=stdin_text, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout + 60, env=env)
    events = []
    for line in proc.stdout.splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if not events:
        try:
            events = [json.loads(proc.stdout)]
        except json.JSONDecodeError:
            raise CallError(f"無法解析輸出 (exit={proc.returncode})：\n{proc.stdout[-1500:]}\n{proc.stderr[-1500:]}",
                            bool(QUOTA_ERROR.search(proc.stdout + proc.stderr)))
    return events, proc.stderr


def call_antigravity(thread: Path, prompt: str, session_id: str | None, opts: dict, work: bool) -> tuple[str, str, str]:
    if len(prompt) > MAX_ARGV_CHARS:
        overflow = thread / f"long_message_{datetime.now():%H%M%S}.md"
        overflow.write_text(prompt, encoding="utf-8")
        prompt = f"這則訊息較長，完整內容在 {overflow}，請先讀取該檔再照內容回覆。"
    timeout = WORK_TIMEOUT if work else TIMEOUT
    cmd = [find_agy(), "-p", prompt, "--output-format", "json", "--print-timeout", f"{timeout}s"]
    if session_id:
        cmd += ["--conversation", session_id]
    if opts.get("model"):
        cmd += ["--model", opts["model"]]
    if opts.get("effort"):
        cmd += ["--effort", opts["effort"]]

    events, stderr = run_cli(cmd, None, timeout)
    result = events[-1]
    reply = (result.get("response") or "").strip()
    if result.get("status") != "SUCCESS" or not reply:
        detail = json.dumps(result, ensure_ascii=False)[:1500] + stderr[-500:]
        raise CallError(f"Antigravity 未產生回覆：{detail}", bool(QUOTA_ERROR.search(detail)),
                        denied=bool(result.get("denied_actions")), session_id=result.get("conversation_id"))
    meta = f"{result.get('duration_seconds', 0):.0f}s, {result.get('usage', {}).get('total_tokens', '?')} tokens"
    return reply, result.get("conversation_id") or session_id, meta


def call_claude(thread: Path, prompt: str, session_id: str | None, opts: dict, work: bool) -> tuple[str, str, str]:
    timeout = WORK_TIMEOUT if work else TIMEOUT
    cmd = [quota.find_claude(), "-p", "--output-format", "stream-json", "--verbose"]
    if work:
        # 交接工作：可改檔，Bash 只開放測試、語法檢查、查額度與唯讀 git；其餘指令在非互動模式下會被拒絕
        cmd += ["--permission-mode", "acceptEdits",
                "--allowedTools", "Bash(python -m unittest*)", "Bash(python -m py_compile*)",
                "Bash(python tools/agora/quota.py*)", "Bash(git status*)", "Bash(git diff*)", "Bash(git log*)",
                "--disallowedTools", "Read(./.env)", "Edit(./.env)"]
    else:
        cmd += ["--disallowedTools", "Edit", "Write", "NotebookEdit"]
    if session_id:
        cmd += ["--resume", session_id]
    if opts.get("model"):
        cmd += ["--model", opts["model"]]

    events, stderr = run_cli(cmd, prompt, timeout)
    for e in events:
        if e.get("type") == "rate_limit_event":
            quota.write_claude_snapshot(quota.windows_from_rate_limit_info(e.get("rate_limit_info") or {}), "agora")
    result = next((e for e in reversed(events) if e.get("type") == "result"), events[-1])
    reply = (result.get("result") or "").strip()
    if result.get("is_error") or not reply:
        detail = json.dumps(result, ensure_ascii=False)[:1500] + stderr[-500:]
        raise CallError(f"Claude 未產生回覆：{detail}", bool(QUOTA_ERROR.search(detail)),
                        denied=bool(result.get("permission_denials")), session_id=result.get("session_id"))
    meta = f"{result.get('duration_ms', 0) / 1000:.0f}s, ${result.get('total_cost_usd', 0):.3f}"
    return reply, result.get("session_id") or session_id, meta


CALLERS = {"claude": call_claude, "antigravity": call_antigravity}


# ───────────────────────── 討論 ─────────────────────────

def build_prompt(state: dict, party: str) -> str:
    info = state["parties"][party]
    unseen = [m for m in state["messages"][info["seen"]:]
              if not (m["speaker"] == party and m["via_cli"])]  # 不重送它自己透過 CLI 產生的回覆
    parts = []
    if not info["session_id"]:
        parts.append(PREAMBLE.format(me=NAMES[party], other=NAMES[OTHER[party]], repo=REPO_ROOT, topic=state["topic"]))
        if unseen:
            parts.append("以下是目前為止的討論內容：")
    for m in unseen:
        parts.append(f"【{NAMES[m['speaker']]} · {m['time']}】\n{m['text']}")
    parts.append(f"請以 {NAMES[party]} 的身分回覆。")
    return "\n\n".join(parts)


def invoke(thread: Path, state: dict, party: str) -> str:
    """請 party 讀取未讀訊息並回覆，回覆寫回討論串（呼叫者需持有 thread_lock）"""
    info = state["parties"][party]
    print(f"…等待 {NAMES[party]} 回覆", file=sys.stderr, flush=True)
    before = git_status()
    try:
        reply, session_id, meta = CALLERS[party](thread, build_prompt(state, party), info["session_id"], info, work=False)
    except (CallError, subprocess.TimeoutExpired, FileNotFoundError) as e:
        add_system_note(thread, f"⚠️ 呼叫 {NAMES[party]} 失敗：{e}")
        save_state(thread, state)
        sys.exit(f"呼叫 {NAMES[party]} 失敗：{e}")
    after = git_status()

    info["session_id"] = session_id
    add_message(thread, state, party, reply, via_cli=True, meta=meta)
    info["seen"] = len(state["messages"])
    save_state(thread, state)
    if before != after:
        warning = f"⚠️ {NAMES[party]} 回覆期間工作區有變動，請檢查 git status：\n{after or '(工作區已乾淨)'}"
        add_system_note(thread, warning)
        print(warning, file=sys.stderr)
    return reply


def guard_nested():
    if os.getenv(NESTED_ENV):
        sys.exit("你是被 Agora 呼叫來的，回覆內容會自動轉給對方，請直接回覆，不要再執行 agora.py。")


def cmd_new(args):
    guard_nested()
    DISCUSSIONS_DIR.mkdir(exist_ok=True)
    thread = DISCUSSIONS_DIR / f"{datetime.now():%Y%m%d-%H%M%S}-{slugify(args.topic)}"
    thread.mkdir()
    state = {
        "topic": args.topic,
        "created": now_str(),
        "parties": {
            "claude": {"session_id": None, "seen": 0, "model": args.claude_model},
            "antigravity": {"session_id": None, "seen": 0, "model": args.agy_model, "effort": args.agy_effort},
        },
        "messages": [],
    }
    save_state(thread, state)
    (thread / "transcript.md").write_text(
        f"# 討論：{args.topic}\n\n- 建立時間：{state['created']}\n- 參與者：Claude Code、Antigravity、Richard\n\n---\n",
        encoding="utf-8")
    print(thread.name)


def cmd_send(args):
    guard_nested()
    thread = resolve_thread(args.thread)
    text = read_message(args)
    target = args.to or (OTHER[args.sender] if args.sender in AI_PARTIES else "none")
    if target == args.sender:
        sys.exit("不能請自己回覆。")
    with thread_lock(thread):
        state = load_state(thread)
        add_message(thread, state, args.sender, text)
        save_state(thread, state)
        if target == "none":
            print("已記錄，尚未請任何一方回覆。")
            return
        print(invoke(thread, state, target))


def cmd_reply(args):
    guard_nested()
    thread = resolve_thread(args.thread)
    with thread_lock(thread):
        print(invoke(thread, load_state(thread), args.party))


def cmd_auto(args):
    guard_nested()
    thread = resolve_thread(args.thread)
    with thread_lock(thread):
        state = load_state(thread)
        last_ai = next((m["speaker"] for m in reversed(state["messages"]) if m["speaker"] in AI_PARTIES), "claude")
        party = args.start or OTHER[last_ai]
        for i in range(args.rounds):
            reply = invoke(thread, state, party)
            print(f"\n═══ 第 {i + 1}/{args.rounds} 輪 · {NAMES[party]} ═══\n{reply}")
            if "【已達成共識】" in reply:
                print("\n（已達成共識，提前結束）")
                break
            party = OTHER[party]


# ───────────────────────── 交接 ─────────────────────────

def relay_paths(thread: Path) -> dict:
    return {"progress": thread / "progress.md", "control": thread / "control.json", "log": thread / "worker.log"}


def update_relay(thread: Path, **fields) -> dict:
    with thread_lock(thread, wait=120):
        state = load_state(thread)
        state.setdefault("relay", {}).update(fields, updated=now_str())
        save_state(thread, state)
        return state["relay"]


def stop_requested(thread: Path) -> bool:
    try:
        return bool(json.loads(relay_paths(thread)["control"].read_text(encoding="utf-8")).get("stop_requested"))
    except Exception:
        return False


def party_usage(party: str) -> dict | None:
    quota.refresh(min_interval=120)
    return quota.claude_usage() if party == "claude" else quota.antigravity_usage()


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True,
                         encoding="utf-8", errors="replace").stdout
    return str(pid) in out


def cmd_relay(args):
    guard_nested()
    thread = resolve_thread(args.thread)
    worker = OTHER[args.sender]
    text = read_message(args)
    state = load_state(thread)
    old = state.get("relay") or {}
    if old.get("status") in ("running", "waiting") and pid_alive(old.get("pid")):
        sys.exit(f"此討論串已有進行中的交接（{NAMES[old['worker']]}），請先 recall。")

    paths = relay_paths(thread)
    paths["control"].write_text(json.dumps({"stop_requested": False}), encoding="utf-8")
    if not paths["progress"].exists():
        paths["progress"].write_text(f"# 工作進度：{state['topic']}\n", encoding="utf-8")
    with open(paths["progress"], "a", encoding="utf-8") as f:
        f.write(f"\n## {now_str()} · {NAMES[args.sender]} 交接給 {NAMES[worker]}\n\n{text.strip()}\n")

    with thread_lock(thread):
        state = load_state(thread)
        add_message(thread, state, args.sender, f"【工作交接 → {NAMES[worker]}】\n{text}")
        state["relay"] = {"from": args.sender, "worker": worker, "status": "starting", "rounds": 0,
                          "session_id": args.session, "handoff": text.strip(), "started": now_str(), "updated": now_str()}
        save_state(thread, state)

    flags = 0
    if sys.platform.startswith("win"):
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    log = open(paths["log"], "a", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "_work", thread.name],
                            cwd=REPO_ROOT, creationflags=flags, stdout=log, stderr=log, stdin=subprocess.DEVNULL)
    update_relay(thread, pid=proc.pid)
    print(f"已交接給 {NAMES[worker]}（背景程序 PID {proc.pid}）。")
    print(f"進度：{paths['progress']}")
    print(f"收回：python tools/agora/agora.py recall {thread.name}")


def sleep_until(thread: Path, ts: float) -> bool:
    """睡到 ts；途中若被 recall 就提早返回 False"""
    while time.time() < ts:
        if stop_requested(thread):
            return False
        time.sleep(min(60, max(1, ts - time.time())))
    return True


def cmd_work(args):
    """背景接手程序（由 relay 啟動）"""
    thread = resolve_thread(args.thread)
    paths = relay_paths(thread)
    relay = load_state(thread)["relay"]
    worker, owner = relay["worker"], relay["from"]
    log = lambda msg: print(f"[{now_str()}] {msg}", flush=True)  # noqa: E731

    denial_retries, denial_note = 0, ""
    for _ in range(MAX_WORK_ROUNDS):
        if stop_requested(thread):
            update_relay(thread, status="stopped", waiting_until=None)
            add_system_note(thread, f"🔁 {NAMES[owner]} 已收回工作，{NAMES[worker]} 停止接手。")
            return

        usage = party_usage(worker)
        if quota.low(usage):
            wake = (usage.get("resets_at") or time.time() + 3600) + 120
            update_relay(thread, status="waiting", waiting_until=wake)
            log(f"{NAMES[worker]} 額度不足，等到 {quota.fmt_time(wake)}")
            add_system_note(thread, f"⏸ {NAMES[worker]} 額度不足，等到 {quota.fmt_time(wake)} 重置後繼續。")
            if not sleep_until(thread, wake):
                continue
            quota.refresh(force=True)
            continue

        relay = update_relay(thread, status="running", waiting_until=None)
        fmt = dict(me=NAMES[worker], other=NAMES[owner], repo=REPO_ROOT, progress=paths["progress"],
                   control=paths["control"], threshold=quota.THRESHOLD_PCT)
        if relay.get("session_id"):
            prompt = CONTINUE_PROMPT.format(**fmt) + denial_note
            if relay["rounds"] == 0:  # 接續舊對話時，本次交接單仍要送到
                prompt += f"\n\n{NAMES[owner]} 的交接單：\n{relay['handoff']}"
        else:
            prompt = WORK_PREAMBLE.format(**fmt) + relay["handoff"]
        denial_note = ""
        state = load_state(thread)
        before = git_status()
        log(f"第 {relay['rounds'] + 1} 輪：呼叫 {NAMES[worker]}")
        try:
            reply, session_id, meta = CALLERS[worker](thread, prompt, relay.get("session_id"), state["parties"][worker], work=True)
        except (CallError, subprocess.TimeoutExpired, FileNotFoundError) as e:
            exhausted = isinstance(e, CallError) and e.quota_exhausted
            log(f"呼叫失敗（額度={exhausted}）：{e}")
            add_system_note(thread, f"⚠️ {NAMES[worker]} 接手時呼叫失敗：{str(e)[:500]}")
            if exhausted or quota.low(party_usage(worker)):
                quota.refresh(force=True)
                continue  # 回到迴圈開頭，進入等待
            if isinstance(e, CallError) and e.denied and denial_retries < 3:
                denial_retries += 1
                if e.session_id:
                    update_relay(thread, session_id=e.session_id)  # 接續同一段對話，保留已讀過的內容
                denial_note = ("\n上一輪因為執行了未授權的終端指令而中斷。列目錄、讀檔、搜尋請改用內建檔案工具，"
                               "終端指令只用工作方式裡列出的那幾個。")
                continue
            update_relay(thread, status="failed", error=str(e)[:500])
            return
        changed = git_status()

        with thread_lock(thread, wait=120):
            state = load_state(thread)
            add_message(thread, state, worker, reply, via_cli=True, meta=f"接手第 {relay['rounds'] + 1} 輪, {meta}")
            state["relay"].update(session_id=session_id, rounds=relay["rounds"] + 1, updated=now_str())
            save_state(thread, state)
        if before != changed:
            add_system_note(thread, f"📝 本輪檔案變動：\n{changed or '(工作區已乾淨)'}")

        if "【工作完成】" in reply:
            update_relay(thread, status="done")
            return
        if "【已暫停】" in reply:
            update_relay(thread, status="stopped")
            return
        # 【額度不足】或沒有結束標記：回到迴圈開頭，視額度決定等待或繼續
    update_relay(thread, status="max_rounds")
    add_system_note(thread, f"⚠️ {NAMES[worker]} 已達最大輪數 {MAX_WORK_ROUNDS}，接手程序結束。")


def cmd_relay_status(args):
    thread = resolve_thread(args.thread)
    deadline = time.time() + (args.wait or 0)
    while True:
        relay = load_state(thread).get("relay")
        if not relay:
            print("此討論串沒有交接紀錄。")
            return
        alive = pid_alive(relay.get("pid"))
        active = alive and relay["status"] in ("starting", "running", "waiting")
        if not active or time.time() >= deadline:
            break
        time.sleep(15)
    waiting = f"，等到 {quota.fmt_time(relay['waiting_until'])}" if relay.get("waiting_until") else ""
    print(f"接手方：{NAMES[relay['worker']]}｜狀態：{relay['status']}{waiting}｜已跑 {relay.get('rounds', 0)} 輪｜"
          f"背景程序：{'執行中' if alive else '已結束'}｜更新：{relay.get('updated')}")
    progress = relay_paths(thread)["progress"]
    if progress.exists():
        blocks = re.split(r"\n(?=## )", progress.read_text(encoding="utf-8"))
        print("\n最新進度：\n" + "\n".join(blocks[-2:]))


def cmd_recall(args):
    guard_nested()
    thread = resolve_thread(args.thread)
    relay_paths(thread)["control"].write_text(json.dumps({"stop_requested": True, "at": now_str()}), encoding="utf-8")
    relay = load_state(thread).get("relay") or {}
    add_system_note(thread, f"🔁 已要求 {NAMES.get(relay.get('worker'), '接手方')} 在下一個檢查點暫停。")
    print("已要求接手方在下一個檢查點暫停。用 relay-status --wait 600 等它停下。")


# ───────────────────────── 其他 ─────────────────────────

def cmd_list(args):
    threads = sorted(p for p in DISCUSSIONS_DIR.glob("*") if (p / "state.json").exists()) if DISCUSSIONS_DIR.exists() else []
    if not threads:
        print("尚無任何討論串。")
    for thread in threads:
        s = load_state(thread)
        last = s["messages"][-1] if s["messages"] else None
        tail = f"最後發言：{NAMES[last['speaker']]} {last['time']}" if last else "尚無發言"
        relay = s.get("relay")
        relay_info = f" | 交接：{NAMES[relay['worker']]} {relay['status']}" if relay else ""
        print(f"{thread.name} | {len(s['messages'])} 則 | {tail}{relay_info} | {s['topic']}")


def cmd_show(args):
    thread = resolve_thread(args.thread)
    text = (thread / "transcript.md").read_text(encoding="utf-8")
    if args.last:
        text = "\n".join(re.split(r"\n(?=### )", text)[-args.last:])
    print(text)


def add_message_args(p):
    p.add_argument("--message")
    p.add_argument("--file")


def main():
    parser = argparse.ArgumentParser(description="Claude Code ⇄ Antigravity 討論與工作交接")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("new", help="建立討論串，輸出討論串 ID")
    p.add_argument("topic")
    p.add_argument("--claude-model", help="回覆用的 Claude 模型，例如 sonnet、opus")
    p.add_argument("--agy-model", help="回覆用的 Antigravity 模型（見 agy models）")
    p.add_argument("--agy-effort", choices=["low", "medium", "high", "xhigh", "max"])
    p.set_defaults(func=cmd_new)

    p = sub.add_parser("send", help="發言，並請另一方回覆")
    p.add_argument("thread")
    p.add_argument("--from", dest="sender", required=True, choices=["claude", "antigravity", "richard"])
    p.add_argument("--to", choices=["claude", "antigravity", "none"],
                   help="由誰回覆；AI 發言預設為另一個 AI，Richard 發言預設 none（只記錄）")
    add_message_args(p)
    p.set_defaults(func=cmd_send)

    p = sub.add_parser("reply", help="不發言，請某方回覆目前未讀的訊息")
    p.add_argument("thread")
    p.add_argument("--party", required=True, choices=AI_PARTIES)
    p.set_defaults(func=cmd_reply)

    p = sub.add_parser("auto", help="兩方自動輪流回覆")
    p.add_argument("thread")
    p.add_argument("--rounds", type=int, default=4)
    p.add_argument("--start", choices=AI_PARTIES, help="由誰先回覆；預設為最後一位 AI 發言者的另一方")
    p.set_defaults(func=cmd_auto)

    p = sub.add_parser("relay", help="把工作交接給另一方在背景執行")
    p.add_argument("thread")
    p.add_argument("--from", dest="sender", required=True, choices=AI_PARTIES)
    p.add_argument("--session", help="接續接手方先前的對話 ID（保留它已讀過的內容）")
    add_message_args(p)
    p.set_defaults(func=cmd_relay)

    p = sub.add_parser("relay-status", help="查看交接狀態與最新進度")
    p.add_argument("thread")
    p.add_argument("--wait", type=int, help="最多等待幾秒，直到接手程序結束")
    p.set_defaults(func=cmd_relay_status)

    p = sub.add_parser("recall", help="請接手方在下一個檢查點暫停")
    p.add_argument("thread")
    p.set_defaults(func=cmd_recall)

    p = sub.add_parser("_work", help=argparse.SUPPRESS)
    p.add_argument("thread")
    p.set_defaults(func=cmd_work)

    p = sub.add_parser("list", help="列出討論串")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("show", help="顯示逐字稿")
    p.add_argument("thread")
    p.add_argument("--last", type=int, help="只顯示最後 N 則")
    p.set_defaults(func=cmd_show)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
