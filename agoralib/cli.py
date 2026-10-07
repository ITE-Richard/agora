"""
Agora：讓 Claude Code、Antigravity、Codex（與使用者）在同一個工作區討論、分派工作、完成任務。

討論：任一方在討論串發言並指定由誰回覆（或全部 AI 依序回覆）；工具呼叫對方的 CLI，
      把對方還沒看過的訊息一次轉過去。每個討論串對每個 AI 各保留一段對話。
分派：assign 把工作交給某一方在背景執行；接手方每完成一小步就寫進度、檢查是否被叫停、
      檢查自己的額度；額度用完會等到重置後再繼續。recall 收回。

討論串存在 <工作區>/.agora/threads/<id>/：state.json（程式用）、transcript.md（給人看）、
progress.md / control.json / worker.log（分派工作時）。
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
from typing import List, Optional

from agoralib import quota
from agoralib.parties import (AGORA_CMD, AGORA_ROOT as AGORA_ROOT_DIR, AI_PARTIES, CALLERS, HUMAN, NAMES,
                              NESTED_ENV, CallError, canonical)

TIMEOUT = int(os.getenv("AGORA_TIMEOUT", "600"))
WORK_TIMEOUT = int(os.getenv("AGORA_WORK_TIMEOUT", "7200"))
MAX_WORK_ROUNDS = int(os.getenv("AGORA_MAX_WORK_ROUNDS", "12"))

PREAMBLE = """\
【Agora 討論環境說明】
你是 {me}，正透過 Agora 與 {others} 進行技術討論，{human} 會閱讀完整逐字稿，也可能插話。
工作區：{workspace}

討論規則：
1. 這是純討論。除非訊息中明確寫出「{human} 已授權修改」，否則不要建立、修改、刪除任何檔案，也不要執行會改變狀態的指令。
   查證論點請用讀檔與搜尋工具，盡量不要執行終端指令（部分 CLI 在這個模式下會拒絕指令並中斷整輪回覆）。
2. 不要自行執行 agora.py 的討論或分派指令；你的回覆會由工具自動轉給其他人。
3. 用繁體中文、一般技術文風回覆。
4. 直接表達立場：同意就說同意並補充，不同意就說明理由；引用程式碼時標出 檔案:行號。對方的論點要先查證再接受。
5. 回覆盡量控制在 600 字內，結尾用一兩句話總結立場或待解問題；認為已達成共識時寫出「【已達成共識】」與共識內容。

討論主題：{topic}
"""

WORK_PREAMBLE = """\
【Agora 工作分派】
你是 {me}。{owner} 把下面的工作交給你（可能因為它額度不足，或依分工指派），請在範圍內完成。
{human} 已授權你在工作單列出的範圍內修改檔案。
工作區：{workspace}

界線（遇到需要越界的情況就停下來，寫進進度檔等 {human} 決定）：
- 只修改工作單範圍內的檔案，不要刪除範圍外的檔案。
- 不要 git commit / push / reset / checkout / stash，不要安裝套件，不要讀寫 .env 或任何憑證。
- 不要執行會影響外部系統的動作（下單、發訊息、部署）。

終端指令只能用以下幾個，而且必須一字不差、不能加任何參數（部分 CLI 的權限是完整比對）：
`{agora} quota`（查額度）、`{agora} check`（工作區的驗證，例如語法檢查與測試）、`git status`、`git diff`、`git log`。
列目錄、讀檔、搜尋、寫檔請用內建的檔案工具。

工作方式：每完成一個小步驟（約 10~20 分鐘的工作量）就做以下三件事：
1. 在 {progress} 末尾追加一段：時間、完成了什麼、改了哪些檔案、下一步是什麼。
2. 讀 {control}；若 "stop_requested" 為 true，把交接說明寫進進度檔後結束這一輪，最後一行寫「【已暫停】」。
3. 執行 `{agora} quota`；若你（{me}）的額度低於 {threshold:.0f}%，把交接說明寫進進度檔後結束，最後一行寫「【額度不足】」。
全部完成時，執行 `{agora} check`，在進度檔寫總結，回覆最後一行寫「【工作完成】」。

以下是 {owner} 的工作單：
"""

CONTINUE_PROMPT = """\
【Agora 工作分派・繼續】
請先讀 {progress} 與 {control}，從上次停下的地方繼續。界線與工作方式和第一輪相同：
每完成一小步就更新進度檔、檢查 control.json、執行 `{agora} quota`；結束時最後一行寫「【工作完成】」「【已暫停】」或「【額度不足】」。
"""


# ───────────────────────── 工作區與討論串 ─────────────────────────

def resolve_workspace(arg: Optional[str]) -> Path:
    if arg:
        return Path(arg).resolve()
    if os.getenv("AGORA_WORKSPACE"):
        return Path(os.environ["AGORA_WORKSPACE"]).resolve()
    try:
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True,
                             encoding="utf-8", timeout=10).stdout.strip()
        if top:
            return Path(top).resolve()
    except Exception:
        pass
    return Path.cwd().resolve()


class Workspace:
    def __init__(self, root: Path):
        self.root = root
        self.home = root / ".agora"
        self.threads = self.home / "threads"

    def config(self) -> dict:
        try:
            return json.loads((self.home / "config.json").read_text(encoding="utf-8"))
        except Exception:
            return {}

    def resolve_thread(self, ref: str) -> Path:
        threads = sorted(p for p in self.threads.glob("*") if (p / "state.json").exists()) \
            if self.threads.exists() else []
        if not threads:
            sys.exit(f"{self.root} 尚無任何討論串，請先執行 new。")
        if ref == "latest":
            return max(threads, key=lambda p: (p / "state.json").stat().st_mtime)
        matches = [p for p in threads if p.name == ref] or [p for p in threads if p.name.startswith(ref)]
        if len(matches) != 1:
            sys.exit(f"找不到唯一符合「{ref}」的討論串：{[p.name for p in matches] or '無'}")
        return matches[0]


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def slugify(text: str) -> str:
    slug = re.sub(r"[^\w一-鿿-]+", "-", text.strip()).strip("-")
    return slug[:40] or "thread"


def load_state(thread: Path) -> dict:
    return json.loads((thread / "state.json").read_text(encoding="utf-8"))


def save_state(thread: Path, state: dict):
    tmp = thread / "state.json.tmp"
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(thread / "state.json")


@contextmanager
def thread_lock(thread: Path, wait: int = 30):
    """同一討論串同時只允許一個呼叫"""
    lock = thread / ".lock"
    deadline = time.time() + wait
    while True:
        try:
            os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            break
        except FileExistsError:
            if time.time() - lock.stat().st_mtime > TIMEOUT + 120:
                lock.unlink(missing_ok=True)
                continue
            if time.time() > deadline:
                sys.exit("此討論串正在等待回覆，請稍後再試。")
            time.sleep(1)
    try:
        yield
    finally:
        lock.unlink(missing_ok=True)


def git_status(root: Path) -> str:
    try:
        return subprocess.run(["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True,
                              encoding="utf-8", timeout=30).stdout
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


def guard_nested():
    if os.getenv(NESTED_ENV):
        sys.exit("你是被 Agora 呼叫來的，回覆內容會自動轉給其他人，請直接回覆，不要再執行 agora.py 的討論或分派指令。")


def thread_parties(state: dict) -> List[str]:
    return list(state["parties"])


# ───────────────────────── 討論 ─────────────────────────

def build_prompt(ws: Workspace, state: dict, party: str) -> str:
    info = state["parties"][party]
    unseen = [m for m in state["messages"][info["seen"]:]
              if not (m["speaker"] == party and m["via_cli"])]
    parts = []
    if not info["session_id"]:
        others = "、".join(NAMES[p] for p in thread_parties(state) if p != party)
        parts.append(PREAMBLE.format(me=NAMES[party], others=others, human=NAMES[HUMAN], workspace=ws.root,
                                     topic=state["topic"]))
        if unseen:
            parts.append("以下是目前為止的討論內容：")
    for m in unseen:
        parts.append(f"【{NAMES.get(m['speaker'], m['speaker'])} · {m['time']}】\n{m['text']}")
    parts.append(f"請以 {NAMES[party]} 的身分回覆。")
    return "\n\n".join(parts)


def invoke(ws: Workspace, thread: Path, state: dict, party: str) -> str:
    """請 party 讀取未讀訊息並回覆（呼叫者需持有 thread_lock）"""
    info = state["parties"][party]
    print(f"…等待 {NAMES[party]} 回覆", file=sys.stderr, flush=True)
    before = git_status(ws.root)
    try:
        reply, session_id, meta = CALLERS[party](build_prompt(ws, state, party), info["session_id"], info,
                                                 False, ws.root, TIMEOUT, thread)
    except (CallError, subprocess.TimeoutExpired, FileNotFoundError) as e:
        add_system_note(thread, f"⚠️ 呼叫 {NAMES[party]} 失敗：{e}")
        save_state(thread, state)
        sys.exit(f"呼叫 {NAMES[party]} 失敗：{e}")
    after = git_status(ws.root)
    info["session_id"] = session_id
    add_message(thread, state, party, reply, via_cli=True, meta=meta)
    info["seen"] = len(state["messages"])
    save_state(thread, state)
    if before != after:
        warning = f"⚠️ {NAMES[party]} 回覆期間工作區有變動，請檢查 git status：\n{after or '(工作區已乾淨)'}"
        add_system_note(thread, warning)
        print(warning, file=sys.stderr)
    return reply


def cmd_new(ws: Workspace, args):
    guard_nested()
    parties = [canonical(p) for p in (args.parties.split(",") if args.parties else AI_PARTIES)]
    bad = [p for p in parties if p not in AI_PARTIES]
    if bad:
        sys.exit(f"未知的參與方：{bad}，可用：{list(AI_PARTIES)}")
    ws.threads.mkdir(parents=True, exist_ok=True)
    thread = ws.threads / f"{datetime.now():%Y%m%d-%H%M%S}-{slugify(args.topic)}"
    thread.mkdir()
    models = dict(m.split("=", 1) for m in (args.model or []))
    state = {
        "topic": args.topic,
        "created": now_str(),
        "parties": {p: {"session_id": None, "seen": 0, "model": models.get(p)} for p in parties},
        "messages": [],
    }
    save_state(thread, state)
    (thread / "transcript.md").write_text(
        f"# 討論：{args.topic}\n\n- 建立時間：{state['created']}\n"
        f"- 參與者：{'、'.join(NAMES[p] for p in parties)}、{NAMES[HUMAN]}\n\n---\n", encoding="utf-8")
    print(thread.name)


def targets_for(state: dict, sender: str, to: Optional[str]) -> List[str]:
    parties = thread_parties(state)
    if to == "none":
        return []
    if to == "all" or (to is None and sender != HUMAN):
        return [p for p in parties if p != sender]
    if to is None:
        return []
    target = canonical(to)
    if target not in parties:
        sys.exit(f"{to} 不在此討論串的參與者中：{parties}")
    if target == sender:
        sys.exit("不能請自己回覆。")
    return [target]


def cmd_send(ws: Workspace, args):
    guard_nested()
    thread = ws.resolve_thread(args.thread)
    sender = canonical(args.sender)
    text = read_message(args)
    with thread_lock(thread):
        state = load_state(thread)
        add_message(thread, state, sender, text)
        save_state(thread, state)
        targets = targets_for(state, sender, args.to)
        if not targets:
            print("已記錄，尚未請任何一方回覆。")
            return
        for party in targets:
            reply = invoke(ws, thread, state, party)
            print(f"\n═══ {NAMES[party]} ═══\n{reply}" if len(targets) > 1 else reply)


def cmd_reply(ws: Workspace, args):
    guard_nested()
    thread = ws.resolve_thread(args.thread)
    with thread_lock(thread):
        print(invoke(ws, thread, load_state(thread), canonical(args.party)))


def cmd_auto(ws: Workspace, args):
    guard_nested()
    thread = ws.resolve_thread(args.thread)
    with thread_lock(thread):
        state = load_state(thread)
        order = [canonical(p) for p in args.order.split(",")] if args.order else thread_parties(state)
        last = next((m["speaker"] for m in reversed(state["messages"]) if m["speaker"] in order), None)
        idx = (order.index(last) + 1) % len(order) if last in order else 0
        for i in range(args.rounds):
            party = order[idx]
            reply = invoke(ws, thread, state, party)
            print(f"\n═══ 第 {i + 1}/{args.rounds} 輪 · {NAMES[party]} ═══\n{reply}")
            if "【已達成共識】" in reply:
                print("\n（已達成共識，提前結束）")
                break
            idx = (idx + 1) % len(order)


# ───────────────────────── 分派工作 ─────────────────────────

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


def party_usage(party: str) -> Optional[dict]:
    quota.refresh(min_interval=120)
    return quota.usage(party)


def pid_alive(pid: Optional[int]) -> bool:
    if not pid:
        return False
    if sys.platform.startswith("win"):
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True,
                             encoding="utf-8", errors="replace").stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def pick_worker(owner: str, candidates: List[str]) -> str:
    """沒指定接手方時，挑剩餘額度最多的另一方"""
    others = [p for p in candidates if p != owner]
    usages = {p: quota.usage(p) for p in others}
    return max(others, key=lambda p: (usages[p] or {}).get("remaining_pct", 0))


def cmd_assign(ws: Workspace, args):
    guard_nested()
    thread = ws.resolve_thread(args.thread)
    owner = canonical(args.sender)
    state = load_state(thread)
    worker = canonical(args.to) if args.to else pick_worker(owner, thread_parties(state))
    if worker not in AI_PARTIES:
        sys.exit(f"接手方必須是 AI：{list(AI_PARTIES)}")
    if worker not in state["parties"]:
        state["parties"][worker] = {"session_id": None, "seen": 0, "model": None}
        save_state(thread, state)
    text = read_message(args)
    old = state.get("relay") or {}
    if old.get("status") in ("starting", "running", "waiting") and pid_alive(old.get("pid")):
        sys.exit(f"此討論串已有進行中的分派（{NAMES[old['worker']]}），請先 recall。")

    paths = relay_paths(thread)
    paths["control"].write_text(json.dumps({"stop_requested": False}), encoding="utf-8")
    if not paths["progress"].exists():
        paths["progress"].write_text(f"# 工作進度：{state['topic']}\n", encoding="utf-8")
    with open(paths["progress"], "a", encoding="utf-8") as f:
        f.write(f"\n## {now_str()} · {NAMES[owner]} 分派給 {NAMES[worker]}\n\n{text.strip()}\n")

    with thread_lock(thread):
        state = load_state(thread)
        add_message(thread, state, owner, f"【工作分派 → {NAMES[worker]}】\n{text}")
        state["relay"] = {"from": owner, "worker": worker, "status": "starting", "rounds": 0,
                          "session_id": args.session, "handoff": text.strip(), "started": now_str(),
                          "updated": now_str()}
        save_state(thread, state)

    flags = 0
    if sys.platform.startswith("win"):
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    log = open(paths["log"], "a", encoding="utf-8")
    entry = Path(__file__).resolve().parents[1] / "agora.py"
    proc = subprocess.Popen([sys.executable, str(entry), "--workspace", str(ws.root), "_work", thread.name],
                            cwd=ws.root, creationflags=flags, stdout=log, stderr=log, stdin=subprocess.DEVNULL)
    update_relay(thread, pid=proc.pid)
    print(f"已分派給 {NAMES[worker]}（背景程序 PID {proc.pid}）。")
    print(f"進度：{paths['progress']}")
    print(f"查詢：python {entry.as_posix()} status {thread.name} --wait 900")


def sleep_until(thread: Path, ts: float) -> bool:
    while time.time() < ts:
        if stop_requested(thread):
            return False
        time.sleep(min(60, max(1, ts - time.time())))
    return True


def cmd_work(ws: Workspace, args):
    """背景接手程序（由 assign 啟動）"""
    thread = ws.resolve_thread(args.thread)
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
        fmt = dict(me=NAMES[worker], owner=NAMES[owner], human=NAMES[HUMAN], workspace=ws.root,
                   progress=paths["progress"], control=paths["control"], threshold=quota.THRESHOLD_PCT,
                   agora=AGORA_CMD)
        if relay.get("session_id"):
            prompt = CONTINUE_PROMPT.format(**fmt) + denial_note
            if relay["rounds"] == 0:
                prompt += f"\n\n{NAMES[owner]} 的工作單：\n{relay['handoff']}"
        else:
            prompt = WORK_PREAMBLE.format(**fmt) + relay["handoff"]
        denial_note = ""
        state = load_state(thread)
        before = git_status(ws.root)
        log(f"第 {relay['rounds'] + 1} 輪：呼叫 {NAMES[worker]}")
        try:
            reply, session_id, meta = CALLERS[worker](prompt, relay.get("session_id"), state["parties"][worker],
                                                      True, ws.root, WORK_TIMEOUT, thread)
        except (CallError, subprocess.TimeoutExpired, FileNotFoundError) as e:
            exhausted = isinstance(e, CallError) and e.quota_exhausted
            log(f"呼叫失敗（額度={exhausted}）：{e}")
            add_system_note(thread, f"⚠️ {NAMES[worker]} 接手時呼叫失敗：{str(e)[:500]}")
            if exhausted or quota.low(party_usage(worker)):
                quota.refresh(force=True)
                continue
            if isinstance(e, CallError) and e.denied and denial_retries < 3:
                denial_retries += 1
                if e.session_id:
                    update_relay(thread, session_id=e.session_id)
                denial_note = ("\n上一輪因為執行了未授權的終端指令而中斷。列目錄、讀檔、搜尋請改用內建檔案工具，"
                               "終端指令只用工作方式裡列出的那幾個，一字不差、不加參數。")
                continue
            update_relay(thread, status="failed", error=str(e)[:500])
            return
        changed = git_status(ws.root)

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
    update_relay(thread, status="max_rounds")
    add_system_note(thread, f"⚠️ {NAMES[worker]} 已達最大輪數 {MAX_WORK_ROUNDS}，接手程序結束。")


def cmd_status(ws: Workspace, args):
    thread = ws.resolve_thread(args.thread)
    deadline = time.time() + (args.wait or 0)
    while True:
        relay = load_state(thread).get("relay")
        if not relay:
            print("此討論串沒有分派紀錄。")
            return
        alive = pid_alive(relay.get("pid"))
        if not (alive and relay["status"] in ("starting", "running", "waiting")) or time.time() >= deadline:
            break
        time.sleep(15)
    waiting = f"，等到 {quota.fmt_time(relay['waiting_until'])}" if relay.get("waiting_until") else ""
    print(f"接手方：{NAMES[relay['worker']]}｜狀態：{relay['status']}{waiting}｜已跑 {relay.get('rounds', 0)} 輪｜"
          f"背景程序：{'執行中' if alive else '已結束'}｜更新：{relay.get('updated')}")
    progress = relay_paths(thread)["progress"]
    if progress.exists():
        blocks = re.split(r"\n(?=## )", progress.read_text(encoding="utf-8"))
        print("\n最新進度：\n" + "\n".join(blocks[-2:]))


def cmd_recall(ws: Workspace, args):
    guard_nested()
    thread = ws.resolve_thread(args.thread)
    relay_paths(thread)["control"].write_text(json.dumps({"stop_requested": True, "at": now_str()}), encoding="utf-8")
    relay = load_state(thread).get("relay") or {}
    add_system_note(thread, f"🔁 已要求 {NAMES.get(relay.get('worker'), '接手方')} 在下一個檢查點暫停。")
    print("已要求接手方在下一個檢查點暫停。用 status --wait 600 等它停下。")


# ───────────────────────── 其他 ─────────────────────────

DEFAULT_CHECK = ["python", "-m", "unittest", "discover", "-s", "tests"]


def cmd_check(ws: Workspace, args):
    """工作區驗證：語法檢查 git 追蹤的 .py，再執行 .agora/config.json 的 check 指令（預設 unittest）"""
    import py_compile
    files = subprocess.run(["git", "ls-files", "*.py"], cwd=ws.root, capture_output=True, text=True,
                           encoding="utf-8").stdout.split()
    files += subprocess.run(["git", "ls-files", "--others", "--exclude-standard", "*.py"], cwd=ws.root,
                            capture_output=True, text=True, encoding="utf-8").stdout.split()
    errors = []
    for f in files:
        try:
            py_compile.compile(str(ws.root / f), doraise=True)
        except py_compile.PyCompileError as e:
            errors.append(str(e))
    print(f"語法檢查：{len(files)} 個檔案，{len(errors)} 個錯誤")
    for e in errors:
        print(e)
    check = ws.config().get("check") or DEFAULT_CHECK
    proc = subprocess.run(check, cwd=ws.root, capture_output=True, text=True, encoding="utf-8", errors="replace")
    keep, in_failure = [], False
    for line in proc.stderr.splitlines() + proc.stdout.splitlines():
        if line.startswith(("ERROR:", "FAIL:")):
            in_failure = True
        if line.startswith("Ran "):
            in_failure = False
        if in_failure or line.startswith(("Ran ", "OK", "FAILED")):
            keep.append(line)
    print(f"驗證指令：{' '.join(check)}（exit={proc.returncode}）")
    print("\n".join(keep[-120:]) or (proc.stdout + proc.stderr)[-3000:])
    sys.exit(1 if errors or proc.returncode else 0)


def cmd_list(ws: Workspace, args):
    threads = sorted(p for p in ws.threads.glob("*") if (p / "state.json").exists()) if ws.threads.exists() else []
    if not threads:
        print(f"{ws.root} 尚無任何討論串。")
    for thread in threads:
        s = load_state(thread)
        last = s["messages"][-1] if s["messages"] else None
        tail = f"最後發言：{NAMES.get(last['speaker'], last['speaker'])} {last['time']}" if last else "尚無發言"
        relay = s.get("relay")
        relay_info = f" | 分派：{NAMES[relay['worker']]} {relay['status']}" if relay else ""
        print(f"{thread.name} | {len(s['messages'])} 則 | {tail}{relay_info} | {s['topic']}")


def cmd_show(ws: Workspace, args):
    text = (ws.resolve_thread(args.thread) / "transcript.md").read_text(encoding="utf-8")
    if args.last:
        text = "\n".join(re.split(r"\n(?=### )", text)[-args.last:])
    print(text)


def cmd_quota(ws: Workspace, args):
    quota.main(args.rest)


def _merge_json(path: Path, update) -> dict:
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            sys.exit(f"{path} 不是合法的 JSON，請先修正。")
    update(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return data


def cmd_install(ws: Workspace, args):
    """把 Agora 裝進工作區：三方使用說明、額度 hook、Antigravity 指令權限、.gitignore、預設驗證設定"""
    import shutil
    root = AGORA_ROOT_DIR
    hook = f"python {(root / 'agoralib' / 'quota_hook.py').as_posix()}"
    done = []

    for agent, dest in (("claude", ws.root / ".claude" / "skills"), ("antigravity", ws.root / ".agents" / "skills")):
        for skill in ("agora", "relay"):
            target = dest / skill
            target.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(root / "skills" / agent / skill / "SKILL.md", target / "SKILL.md")
        done.append(f"{agent} skills → {dest}")

    # Codex（以及同樣會讀 AGENTS.md 的 Antigravity）：以標記區段寫入，重裝時整段替換
    agents_md = ws.root / "AGENTS.md"
    section = (root / "skills" / "codex" / "AGENTS.md").read_text(encoding="utf-8").strip()
    text = agents_md.read_text(encoding="utf-8") if agents_md.exists() else ""
    text = re.sub(r"<!-- agora:begin -->.*?<!-- agora:end -->", lambda _: section, text, flags=re.S) \
        if "<!-- agora:begin -->" in text else (text.rstrip() + "\n\n" + section if text.strip() else section)
    agents_md.write_text(text.rstrip() + "\n", encoding="utf-8")
    done.append(f"Codex 說明 → {agents_md}")

    def claude_hooks(data):
        hooks = data.setdefault("hooks", {})
        for event, mode, matcher in (("UserPromptSubmit", "claude-prompt", None), ("PostToolUse", "claude-tool", "*")):
            groups = [g for g in hooks.get(event, []) if "quota_hook.py" not in json.dumps(g)]
            group = {"hooks": [{"type": "command", "command": f"{hook} {mode}", "timeout": 15}]}
            if matcher:
                group["matcher"] = matcher
            hooks[event] = groups + [group]
    _merge_json(ws.root / ".claude" / "settings.local.json", claude_hooks)
    done.append("Claude hooks → .claude/settings.local.json（個人設定，不進 git）")

    def agy_hooks(data):
        data["agora-quota"] = {"PreInvocation": [{"type": "command", "command": f"{hook} agy-pre", "timeout": 15}]}
    _merge_json(ws.root / ".agents" / "hooks.json", agy_hooks)
    done.append("Antigravity hook → .agents/hooks.json")

    # Antigravity CLI 的權限是全域、完整比對：只開放接手方需要的固定指令與此工作區的寫入
    def agy_permissions(data):
        allow = data.setdefault("permissions", {}).setdefault("allow", [])
        for rule in (f"command({AGORA_CMD} quota)", f"command({AGORA_CMD} check)", "command(git status)",
                     "command(git diff)", "command(git log)", f"write_file({ws.root})", f"write_file({ws.root.as_posix()})",
                     f"read_file({AGORA_ROOT_DIR})", f"read_file({AGORA_ROOT_DIR.as_posix()})"):
            if rule not in allow:
                allow.append(rule)
    _merge_json(Path.home() / ".gemini" / "antigravity-cli" / "settings.json", agy_permissions)
    done.append("Antigravity 權限 → ~/.gemini/antigravity-cli/settings.json")

    gitignore = ws.root / ".gitignore"
    lines = gitignore.read_text(encoding="utf-8").splitlines() if gitignore.exists() else []
    for entry in (".agora/", ".claude/settings.local.json"):
        if entry not in lines:
            lines.append(entry)
    gitignore.write_text("\n".join(lines) + "\n", encoding="utf-8")
    done.append(".gitignore 加入 .agora/、.claude/settings.local.json")

    config = ws.home / "config.json"
    if not config.exists():
        ws.home.mkdir(parents=True, exist_ok=True)
        config.write_text(json.dumps({"check": DEFAULT_CHECK}, indent=2) + "\n", encoding="utf-8")
        done.append(f"預設驗證設定 → {config}")

    print(f"已將 Agora 安裝到 {ws.root}：")
    for d in done:
        print(f"  • {d}")
    print("Claude Code 需重新載入視窗（或開啟 /hooks）才會套用新的 hook。")


def add_message_args(p):
    p.add_argument("--message")
    p.add_argument("--file")


def main(argv=None):
    if sys.platform.startswith("win"):
        for stream in (sys.stdout, sys.stderr, sys.stdin):
            try:
                stream.reconfigure(encoding="utf-8")
            except Exception:
                pass
    parser = argparse.ArgumentParser(prog="agora", description="Claude Code ⇄ Antigravity ⇄ Codex 討論與分派工作")
    parser.add_argument("--workspace", help="工作區（預設：目前資料夾的 git 根目錄）")
    sub = parser.add_subparsers(dest="command", required=True)
    who = list(AI_PARTIES) + [HUMAN, "richard"]

    p = sub.add_parser("new", help="建立討論串，輸出 ID")
    p.add_argument("topic")
    p.add_argument("--parties", help="參與的 AI，逗號分隔（預設全部：claude,antigravity,codex）")
    p.add_argument("--model", action="append", help="指定模型，例如 --model claude=sonnet --model codex=gpt-5")
    p.set_defaults(func=cmd_new)

    p = sub.add_parser("send", help="發言；AI 發言預設請其他所有 AI 依序回覆，人類發言預設只記錄")
    p.add_argument("thread")
    p.add_argument("--from", dest="sender", required=True, choices=who)
    p.add_argument("--to", help="claude / antigravity / codex / all / none")
    add_message_args(p)
    p.set_defaults(func=cmd_send)

    p = sub.add_parser("reply", help="不發言，請某方回覆未讀訊息")
    p.add_argument("thread")
    p.add_argument("--party", required=True, choices=AI_PARTIES)
    p.set_defaults(func=cmd_reply)

    p = sub.add_parser("auto", help="AI 依序自動發言")
    p.add_argument("thread")
    p.add_argument("--rounds", type=int, default=6)
    p.add_argument("--order", help="發言順序，逗號分隔（預設討論串參與者順序）")
    p.set_defaults(func=cmd_auto)

    for name in ("assign", "relay"):
        p = sub.add_parser(name, help="把工作分派給某一方在背景執行" + ("（assign 的別名）" if name == "relay" else ""))
        p.add_argument("thread")
        p.add_argument("--from", dest="sender", required=True, choices=who)
        p.add_argument("--to", help="接手方（預設：剩餘額度最多的另一方）")
        p.add_argument("--session", help="接續接手方先前的對話 ID")
        add_message_args(p)
        p.set_defaults(func=cmd_assign)

    for name in ("status", "relay-status"):
        p = sub.add_parser(name, help="查看分派狀態與最新進度")
        p.add_argument("thread")
        p.add_argument("--wait", type=int, help="最多等待幾秒，直到接手程序結束")
        p.set_defaults(func=cmd_status)

    p = sub.add_parser("recall", help="請接手方在下一個檢查點暫停")
    p.add_argument("thread")
    p.set_defaults(func=cmd_recall)

    p = sub.add_parser("check", help="工作區驗證（語法檢查＋.agora/config.json 的 check 指令）")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("quota", help="三方剩餘額度（--refresh、--json）")
    p.add_argument("rest", nargs=argparse.REMAINDER)
    p.set_defaults(func=cmd_quota)

    p = sub.add_parser("install", help="把 Agora 裝進工作區（skills、hooks、權限、.gitignore）")
    p.set_defaults(func=cmd_install)

    p = sub.add_parser("_work", help=argparse.SUPPRESS)
    p.add_argument("thread")
    p.set_defaults(func=cmd_work)

    p = sub.add_parser("list", help="列出討論串")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("show", help="顯示逐字稿")
    p.add_argument("thread")
    p.add_argument("--last", type=int)
    p.set_defaults(func=cmd_show)

    args = parser.parse_args(argv)
    ws = Workspace(resolve_workspace(args.workspace))
    os.environ.setdefault("AGORA_WORKSPACE", str(ws.root))
    args.func(ws, args)
