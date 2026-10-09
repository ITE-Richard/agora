"""
Agora：讓 Claude Code、Antigravity、Codex（與使用者）在同一個工作區討論、分派工作、完成任務。

討論：任一方在討論串發言並指定由誰回覆（或全部 AI 依序回覆）；工具呼叫對方的 CLI，
      把對方還沒看過的訊息一次轉過去。每個討論串對每個 AI 各保留一段對話。
分派：assign 把工作交給某一方在背景執行；接手方每完成一小步就寫進度、檢查是否被叫停、
      檢查自己的額度；額度用完會等到重置後再繼續。recall 收回。

討論與分派都在獨立的背景程序執行（見 jobs.py），呼叫者被終止（例如關掉 VSCode 視窗）時不受影響；
呼叫者只負責顯示進度，中斷後可用 wait 重新接上。

討論串存在 <工作區>/.agora/threads/<id>/：state.json（程式用）、transcript.md（給人看）、
job.json / job.log（討論的背景程序）、progress.md / control.json / worker.log（分派工作時）、
live/discussion.md、live/work.md（回覆進行中的內容，完成後刪除）。
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

from agoralib import __version__, config, jobs, models, prune, quota, shares, snapshot
from agoralib.parties import (AGORA_ROOT as AGORA_ROOT_DIR, AI_PARTIES, CALLERS, HUMAN, NAMES,
                              NESTED_ENV, CallError, Live, agora_cmd, canonical,
                              quote_path)

TIMEOUT = int(os.getenv("AGORA_TIMEOUT", "600"))
WORK_TIMEOUT = int(os.getenv("AGORA_WORK_TIMEOUT", "7200"))
MAX_WORK_ROUNDS = int(os.getenv("AGORA_MAX_WORK_ROUNDS", "12"))
CONSENSUS = "【已達成共識】"

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
   要所有參與的 AI 都接連寫出「【已達成共識】」才算達成；你仍有異議時就不要寫，並說明還差什麼。

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

SUMMARY_PROMPT = """\
【Agora 討論總結】
你是 {me}。請閱讀下面「{topic}」的完整討論逐字稿（共 {count} 則），整理成總結。
這是純整理：不要建立、修改、刪除任何檔案，也不要執行會改變狀態的指令；需要查證時用讀檔與搜尋工具。
工作區：{workspace}

請嚴格依照以下格式輸出，標題文字不要改；沒有內容的段落寫「無」：

## 共識
## 採納的意見
（誰提出、為什麼採納）
## 放棄的意見
（誰提出、為什麼放棄）
## 保留的異議
（尚未解決的分歧與各方理由）
## 工作單
### 範圍
（要修改的檔案，用工作區相對路徑，例如 `src/app.py`；以及不能碰的檔案）
### 步驟
### 驗收條件
（可以執行的驗證方式，例如要通過的測試）

工作單要能直接交給另一個 AI 執行；討論沒有結論、不適合分派時，「## 工作單」底下只寫「無」。

以下是完整逐字稿：
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
        return config.load(self.root)

    def enabled(self) -> List[str]:
        return config.enabled(self.root)

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
            try:
                if time.time() - lock.stat().st_mtime > TIMEOUT + 120:
                    lock.unlink(missing_ok=True)
                    continue
            except OSError:
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


def parse_pairs(items: Optional[List[str]]) -> dict:
    """["codex=gpt-5", "claude=opus"] → {"codex": "gpt-5", "claude": "opus"}"""
    out = {}
    for item in items or []:
        if "=" not in item:
            sys.exit(f"格式應為 <AI>=<值>：{item}")
        party, value = item.split("=", 1)
        party = canonical(party)
        if party not in AI_PARTIES:
            sys.exit(f"未知的 AI：{party}，可用：{list(AI_PARTIES)}")
        out[party] = value.strip()
    return out


def guard_nested():
    if os.getenv(NESTED_ENV):
        sys.exit("你是被 Agora 呼叫來的，回覆內容會自動轉給其他人，請直接回覆，不要再執行 agora.py 的討論或分派指令。")


def remember_session(state: dict, party: str, session_id: Optional[str], external: bool = False):
    """記下 Agora 在各 CLI 開過的對話，清理討論串時一併刪除（external：使用者帶進來的對話，不刪）"""
    sessions = state.setdefault("sessions", [])
    if session_id and not any(x["id"] == session_id for x in sessions):
        sessions.append({"party": party, "id": session_id, "external": external})


def new_party(model: Optional[str] = None, effort: Optional[str] = None) -> dict:
    return {"session_id": None, "seen": 0, "model": model, "effort": effort}


def thread_parties(state: dict, enabled=AI_PARTIES) -> List[str]:
    """討論串中目前仍在專案啟用名單內的 AI"""
    return [p for p in state["parties"] if p in enabled]


def require_enabled(ws: Workspace, party: str) -> str:
    enabled = ws.enabled()
    if party not in AI_PARTIES:
        sys.exit(f"未知的 AI：{party}，可用：{list(AI_PARTIES)}")
    if party not in enabled:
        sys.exit(f"{NAMES[party]} 未在此專案啟用（目前啟用：{', '.join(enabled)}；"
                 f"可用 agora parties --enable {party} 加入）。")
    return party


def call_opts(ws: Workspace, state: dict, party: str) -> dict:
    """模型與推理強度：討論串自己指定的優先，其次是專案設定，都沒有就用 CLI 預設"""
    info = state["parties"].get(party) or {}
    settings = config.party_settings(ws.root)[party]
    return {f: info.get(f) or settings.get(f) for f in config.FIELDS}


# ───────────────────────── 討論 ─────────────────────────

def build_prompt(ws: Workspace, state: dict, party: str) -> str:
    info = state["parties"][party]
    unseen = [m for m in state["messages"][info["seen"]:]
              if not (m["speaker"] == party and m["via_cli"])]
    parts = []
    if not info["session_id"]:
        others = "、".join(NAMES[p] for p in thread_parties(state, ws.enabled()) if p != party)
        parts.append(PREAMBLE.format(me=NAMES[party], others=others, human=NAMES[HUMAN], workspace=ws.root,
                                     topic=state["topic"]))
        if unseen:
            parts.append("以下是目前為止的討論內容：")
    for m in unseen:
        parts.append(f"【{NAMES.get(m['speaker'], m['speaker'])} · {m['time']}】\n{m['text']}")
    parts.append(f"請以 {NAMES[party]} 的身分回覆。")
    return "\n\n".join(parts)


def live_path(thread: Path, kind: str) -> Path:
    """回覆進行中的內容（kind：discussion 為討論與總結、work 為接手工作）"""
    return thread / "live" / f"{kind}.md"


DENIED_RETRY = ("上一輪你使用了不被允許的工具或終端指令，整輪回覆被中斷。這是純討論：請改用內建的讀檔、搜尋工具查證，"
                "不要執行任何終端指令、不要修改檔案，然後直接回覆上面的訊息。")


class CallFailed(SystemExit):
    """某一方沒有回覆（已寫進逐字稿）；send 會繼續請其他人回覆，其他指令照常結束"""


def invoke(ws: Workspace, thread: Path, state: dict, party: str) -> str:
    """請 party 讀取未讀訊息並回覆（呼叫者需持有 thread_lock）。
    因為用了不被允許的工具而整輪中斷時（部分 CLI 在非互動模式會這樣），提醒它改用讀檔工具，接續同一段對話重試一次。"""
    info = state["parties"].setdefault(party, new_party())   # 討論串建立後才啟用的 AI 也能加入
    opts = call_opts(ws, state, party)
    model = "、".join(v for v in opts.values() if v)
    print(f"…等待 {NAMES[party]}{f'（{model}）' if model else ''} 回覆", file=sys.stderr, flush=True)
    jobs.set_current(party)
    before = git_status(ws.root)
    prompt, session = build_prompt(ws, state, party), info["session_id"]
    for attempt in (1, 2):
        live = Live(live_path(thread, "discussion"), NAMES[party], ws.root)
        try:
            reply, session_id, meta = CALLERS[party](prompt, session, opts, False, ws.root, TIMEOUT, thread, live=live)
            break
        except (CallError, subprocess.TimeoutExpired, FileNotFoundError) as e:
            live.close()
            remember_session(state, party, getattr(e, "session_id", None))
            if attempt == 1 and isinstance(e, CallError) and e.denied:
                note = f"⚠️ {NAMES[party]} 用了不被允許的工具或終端指令，整輪被中斷；已提醒改用讀檔工具，重試一次。"
                add_system_note(thread, note)
                print(note, file=sys.stderr, flush=True)
                if e.session_id:   # 接續被中斷的那段對話（它已經讀過訊息），只送提醒
                    prompt, session = DENIED_RETRY, e.session_id
                else:
                    prompt += "\n\n" + DENIED_RETRY
                continue
            add_system_note(thread, f"⚠️ 呼叫 {NAMES[party]} 失敗：{e}")
            save_state(thread, state)
            raise CallFailed(f"呼叫 {NAMES[party]} 失敗：{e}")
    live.close()
    after = git_status(ws.root)
    info["session_id"] = session_id
    remember_session(state, party, session_id)
    add_message(thread, state, party, reply, via_cli=True, meta=", ".join(x for x in (model, meta) if x))
    info["seen"] = len(state["messages"])
    save_state(thread, state)
    if before != after:
        warning = f"⚠️ {NAMES[party]} 回覆期間工作區有變動，請檢查 git status：\n{after or '(工作區已乾淨)'}"
        add_system_note(thread, warning)
        print(warning, file=sys.stderr)
    return reply


def cmd_new(ws: Workspace, args):
    guard_nested()
    parties = [require_enabled(ws, canonical(p)) for p in args.parties.split(",")] if args.parties else ws.enabled()
    ws.threads.mkdir(parents=True, exist_ok=True)
    thread = ws.threads / f"{datetime.now():%Y%m%d-%H%M%S}-{slugify(args.topic)}"
    thread.mkdir()
    models, efforts = parse_pairs(args.model), parse_pairs(args.effort)
    state = {
        "topic": args.topic,
        "created": now_str(),
        "parties": {p: new_party(models.get(p), efforts.get(p)) for p in parties},
        "messages": [],
    }
    save_state(thread, state)
    (thread / "transcript.md").write_text(
        f"# 討論：{args.topic}\n\n- 建立時間：{state['created']}\n"
        f"- 參與者：{'、'.join(NAMES[p] for p in parties)}、{NAMES[HUMAN]}\n\n---\n", encoding="utf-8")
    print(thread.name)


def targets_for(state: dict, sender: str, to: Optional[str], enabled=AI_PARTIES) -> List[str]:
    parties = thread_parties(state, enabled)
    if to == "none":
        return []
    if to == "all" or (to is None and sender != HUMAN):
        return [p for p in parties if p != sender]
    if to is None:
        return []
    target = canonical(to)
    if target not in enabled:
        sys.exit(f"{to} 未在此專案啟用：{list(enabled)}")
    if target == sender:
        sys.exit("不能請自己回覆。")
    return [target]


def in_background(ws: Workspace, thread: Path, kind: str, argv: List[str], text: Optional[str] = None):
    """在背景程序執行這個討論指令並把輸出轉到目前的終端；已在背景程序內則回傳，由呼叫者直接執行"""
    if os.getenv(jobs.JOB_ENV):
        return
    job = jobs.active(thread)
    if job:
        sys.exit(f"此討論串已有進行中的討論（{NAMES.get(job.get('current'), '啟動中')}），"
                 f"用 agora wait {thread.name} 查看，或 agora stop {thread.name} 停止。")
    lock = thread / ".lock"
    try:
        if lock.exists() and time.time() - lock.stat().st_mtime > 30 and not state_relay_active(thread):
            lock.unlink(missing_ok=True)   # 上一個討論程序被強制結束時留下的鎖
    except OSError:
        pass
    if text is not None:
        (thread / "job_input.md").write_text(text, encoding="utf-8")
        argv = argv + ["--file", str(thread / "job_input.md")]
    entry = AGORA_ROOT_DIR / "agora.py"
    jobs.start(thread, kind, [sys.executable, str(entry), "--workspace", str(ws.root)] + argv, ws.root)
    sys.exit(jobs.follow(thread))


def state_relay_active(thread: Path) -> bool:
    relay = load_state(thread).get("relay") or {}
    return relay.get("status") in ("starting", "running", "waiting") and pid_alive(relay.get("pid"))


def cmd_send(ws: Workspace, args):
    guard_nested()
    thread = ws.resolve_thread(args.thread)
    sender = canonical(args.sender)
    text = read_message(args)
    in_background(ws, thread, "send", ["send", thread.name, "--from", sender] + (["--to", args.to] if args.to else []),
                  text)
    with thread_lock(thread):
        state = load_state(thread)
        add_message(thread, state, sender, text)
        save_state(thread, state)
        targets = targets_for(state, sender, args.to, ws.enabled())
        if not targets:
            print("已記錄，尚未請任何一方回覆。")
            return
        failed = []
        for party in targets:
            try:   # 某一方失敗時繼續請其他人回覆
                reply = invoke(ws, thread, state, party)
            except CallFailed as e:
                failed.append(party)
                print(e.code, file=sys.stderr, flush=True)
                continue
            print(f"\n═══ {NAMES[party]} ═══\n{reply}" if len(targets) > 1 else reply)
        if failed:
            sys.exit(f"{'、'.join(NAMES[p] for p in failed)} 沒有回覆，原因已寫進逐字稿。")


def cmd_reply(ws: Workspace, args):
    guard_nested()
    thread = ws.resolve_thread(args.thread)
    party = require_enabled(ws, canonical(args.party))
    in_background(ws, thread, "reply", ["reply", thread.name, "--party", party])
    with thread_lock(thread):
        print(invoke(ws, thread, load_state(thread), party))


def agree(agreed: set, party: str, reply: str) -> set:
    """共識要所有發言方連續表示同意：有人沒寫「【已達成共識】」就重新計算"""
    return agreed | {party} if CONSENSUS in reply else set()


def cmd_auto(ws: Workspace, args):
    guard_nested()
    thread = ws.resolve_thread(args.thread)
    in_background(ws, thread, "auto", ["auto", thread.name, "--rounds", str(args.rounds)]
                  + (["--order", args.order] if args.order else []))
    with thread_lock(thread):
        state = load_state(thread)
        order = [require_enabled(ws, canonical(p)) for p in args.order.split(",")] if args.order \
            else thread_parties(state, ws.enabled())
        if not order:
            sys.exit("此討論串沒有任何啟用中的 AI。")
        last = next((m["speaker"] for m in reversed(state["messages"]) if m["speaker"] in order), None)
        idx = (order.index(last) + 1) % len(order) if last in order else 0
        agreed = set()
        for i in range(args.rounds):
            party = order[idx]
            reply = invoke(ws, thread, state, party)
            print(f"\n═══ 第 {i + 1}/{args.rounds} 輪 · {NAMES[party]} ═══\n{reply}")
            agreed = agree(agreed, party, reply)
            if agreed >= set(order):
                print("\n（各方都表示已達成共識，提前結束）")
                break
            if agreed:
                print(f"\n（{'、'.join(NAMES[p] for p in order if p in agreed)} 認為已達成共識，等其他方確認）")
            idx = (idx + 1) % len(order)


# ───────────────────────── 總結 ─────────────────────────

WORK_ORDER_HEADING = re.compile(r"^##[ \t]*工作單[ \t]*$", re.M)
PATH_TOKEN = re.compile(r"(?<![\w/.\\-])((?:[\w.-]+[/\\])*[\w-][\w.-]*\.[A-Za-z0-9]+)")


def work_order(summary: str) -> str:
    """總結的「## 工作單」段落（到下一個 ## 標題為止）；找不到或是空的就丟出 ValueError"""
    m = WORK_ORDER_HEADING.search(summary)
    if not m:
        raise ValueError("總結裡找不到「## 工作單」段落")
    rest = summary[m.end():]
    end = re.search(r"^##(?!#)", rest, re.M)
    body = (rest[:end.start()] if end else rest).strip()
    if not body or body.strip("（）() ") == "無":
        raise ValueError("工作單是空的（討論沒有可以分派的結論）")
    return body


def file_hash(path: Path) -> Optional[str]:
    import hashlib
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def referenced_files(root: Path, text: str) -> dict:
    """工作單提到的工作區檔案 → 內容雜湊（不存在的檔案記為 None），用來偵測總結是否過期"""
    out, top = {}, root.resolve()
    for token in PATH_TOKEN.findall(text):
        rel = token.replace("\\", "/")
        rel = rel[2:] if rel.startswith("./") else rel
        path = (top / rel).resolve()
        # 存在的檔案，或之後才要建立的檔案（含目錄，或副檔名是英文字母開頭，例如 new_module.py）；
        # 版本號（0.1.1）之類的不算
        new_file = not path.exists() and ("/" in rel or re.search(r"\.[A-Za-z]\w*$", rel))
        if top in path.parents and (path.is_file() or new_file):
            out[rel] = file_hash(path)
    return out


def stale_reasons(root: Path, state: dict) -> List[str]:
    info = state.get("summary") or {}
    reasons = []
    newer = len(state["messages"]) - info.get("messages", 0)
    if newer > 0:
        reasons.append(f"總結後又有 {newer} 則新訊息")
    changed = [p for p, h in (info.get("files") or {}).items() if file_hash(root / p) != h]
    if changed:
        reasons.append("工作單涉及的檔案已變動：" + "、".join(changed[:5]) + ("…" if len(changed) > 5 else ""))
    return reasons


def suggest_other(ws: Workspace, party: str) -> Optional[str]:
    others = [p for p in ws.enabled() if p != party and not quota.low(quota.usage(p))]
    return max(others, key=lambda p: (quota.usage(p) or {}).get("remaining_pct", 0)) if others else None


def cmd_summarize(ws: Workspace, args):
    """請指定的一方用新的對話讀完整逐字稿，產出共識、取捨、異議與工作單（存成 summary.md）"""
    guard_nested()
    thread = ws.resolve_thread(args.thread)
    by = args.by or ws.config().get("summarizer")
    if not by:
        sys.exit("請用 --by 指定總結方（或在 .agora/config.json 設定 \"summarizer\"）。")
    party = require_enabled(ws, canonical(by))
    usage = quota.usage(party)
    if quota.low(usage):
        other = suggest_other(ws, party)
        sys.exit(f"{NAMES[party]} 額度不足（剩 {usage.get('remaining_pct', 0):.0f}%），"
                 + (f"可改用 --by {other}。" if other else "其他 AI 的額度也不足。"))
    in_background(ws, thread, "summarize", ["summarize", thread.name, "--by", party])
    with thread_lock(thread):
        state = load_state(thread)
        messages = state["messages"]
        if not messages:
            sys.exit("此討論串還沒有任何發言。")
        transcript = "\n\n".join(f"【{NAMES.get(m['speaker'], m['speaker'])} · {m['time']}】\n{m['text']}"
                                 for m in messages)
        prompt = SUMMARY_PROMPT.format(me=NAMES[party], topic=state["topic"], count=len(messages),
                                       workspace=ws.root) + "\n" + transcript
        opts = call_opts(ws, state, party)
        print(f"…等待 {NAMES[party]} 產出總結", file=sys.stderr, flush=True)
        jobs.set_current(party)
        live = Live(live_path(thread, "discussion"), f"{NAMES[party]}（總結）", ws.root)
        try:   # 新的對話（不續接討論的 session），確保讀到完整逐字稿
            reply, session_id, meta = CALLERS[party](prompt, None, opts, False, ws.root, TIMEOUT, thread, live=live)
        except (CallError, subprocess.TimeoutExpired, FileNotFoundError) as e:
            live.close()
            add_system_note(thread, f"⚠️ {NAMES[party]} 產出總結失敗：{e}")
            remember_session(state, party, getattr(e, "session_id", None))
            save_state(thread, state)
            sys.exit(f"{NAMES[party]} 產出總結失敗：{e}")
        live.close()
        remember_session(state, party, session_id)
        try:
            order, error = work_order(reply), None
        except ValueError as e:
            order, error = "", str(e)
        add_message(thread, state, party, f"【討論總結】\n{reply}", meta=f"總結（新對話）, {meta}")
        state["summary"] = {"by": party, "time": now_str(), "messages": len(state["messages"]),
                            "files": referenced_files(ws.root, order), "error": error}
        save_state(thread, state)
        (thread / "summary.md").write_text(
            f"# 總結：{state['topic']}\n\n- 總結方：{NAMES[party]}\n- 時間：{state['summary']['time']}\n"
            f"- 涵蓋 {len(messages)} 則訊息\n\n{reply.strip()}\n", encoding="utf-8")
    print(reply)
    if error:
        print(f"\n⚠️ {error}，無法依此總結分派。", file=sys.stderr)


def summary_handoff(ws: Workspace, thread: Path, state: dict, confirmed: bool) -> str:
    """assign --from-summary：取總結的工作單；解析失敗就拒絕，過期時要 --yes 確認"""
    info = state.get("summary")
    if not info or not (thread / "summary.md").exists():
        sys.exit("此討論串還沒有總結，請先執行 summarize。")
    try:
        order = work_order((thread / "summary.md").read_text(encoding="utf-8"))
    except ValueError as e:
        sys.exit(f"無法依總結分派：{e}。")
    reasons = stale_reasons(ws.root, state)
    if reasons and not confirmed:
        sys.exit("總結可能已過期：" + "；".join(reasons) + "。確認仍要依此總結分派，請加 --yes。")
    return f"（依 {NAMES.get(info['by'], info['by'])} 在 {info['time']} 的討論總結）\n\n{order}"


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


pid_alive = jobs.pid_alive


def pick_worker(ws: Workspace, owner: str, candidates: List[str]) -> str:
    """沒指定接手方時依分配比例挑選（見 shares.py）；大家額度都不足時挑額度最多的，等它重置後開始"""
    weights = config.shares(ws.root)
    others = [p for p in candidates if p != owner and weights[p] > 0]
    if not others:
        sys.exit("沒有可自動分派的 AI（其他啟用中 AI 的分配比例都是 0），請用 --to 指定接手方。")
    usages = {p: quota.usage(p) for p in others}
    work = shares.recent_work(ws.root)
    worker = shares.pick(others, weights, work, usages) \
        or max(others, key=lambda p: (usages[p] or {}).get("remaining_pct", 0))
    total = sum(weights[p] for p in others)
    print(f"依分配比例挑選 {NAMES[worker]}（" + "、".join(
        f"{NAMES[p]} 目標 {int(weights[p] / total * 100 + 0.5)}% 近 {shares.SHARE_DAYS:g} 天 {work.get(p, 0)} 輪" for p in others) + "）")
    return worker


def cmd_assign(ws: Workspace, args):
    guard_nested()
    thread = ws.resolve_thread(args.thread)
    owner = canonical(args.sender)
    state = load_state(thread)
    if args.from_summary:
        if args.message is not None or args.file:
            sys.exit("--from-summary 不能和 --message / --file 一起使用。")
        text = summary_handoff(ws, thread, state, args.yes)
    else:
        text = read_message(args)
    worker = require_enabled(ws, canonical(args.to)) if args.to else pick_worker(ws, owner, ws.enabled())
    if worker not in state["parties"]:
        state["parties"][worker] = new_party()
        save_state(thread, state)
    old = state.get("relay") or {}
    if old.get("status") in ("starting", "running", "waiting") and pid_alive(old.get("pid")):
        sys.exit(f"此討論串已有進行中的分派（{NAMES[old['worker']]}），請先 recall。")

    paths = relay_paths(thread)
    paths["control"].write_text(json.dumps({"stop_requested": False}), encoding="utf-8")
    if not paths["progress"].exists():
        paths["progress"].write_text(f"# 工作進度：{state['topic']}\n", encoding="utf-8")
    with open(paths["progress"], "a", encoding="utf-8") as f:
        f.write(f"\n## {now_str()} · {NAMES[owner]} 分派給 {NAMES[worker]}\n\n{text.strip()}\n")

    # 分派基準：之後用 agora changes 比對分派期間的變更（不影響使用者的 index 與工作區）
    try:
        baseline = snapshot.create(ws.root, thread.name)
    except (snapshot.SnapshotError, OSError, subprocess.SubprocessError) as e:
        baseline = {"error": str(e)}

    with thread_lock(thread):
        state = load_state(thread)
        add_message(thread, state, owner, f"【工作分派 → {NAMES[worker]}】\n{text}")
        state["relay"] = {"from": owner, "worker": worker, "status": "starting", "rounds": 0,
                          "session_id": args.session, "external_session": args.session,
                          "handoff": text.strip(), "started": now_str(),
                          "updated": now_str(), "baseline": baseline}
        save_state(thread, state)

    entry = AGORA_ROOT_DIR / "agora.py"
    pid = jobs.spawn([sys.executable, str(entry), "--workspace", str(ws.root), "_work", thread.name],
                     ws.root, paths["log"], paths["log"], append=True)
    update_relay(thread, pid=pid)
    print(f"已分派給 {NAMES[worker]}（背景程序 PID {pid}，關閉視窗也會繼續）。")
    if baseline.get("error"):
        print(f"⚠️ 沒有記錄分派基準，無法用 changes 檢視變更：{baseline['error']}")
    print(f"進度：{paths['progress']}")
    print(f"查詢：{agora_cmd()} status {thread.name} --wait 900")


def cmd_changes(ws: Workspace, args):
    """分派期間的變更：相對於 assign 當下的工作區快照（期間任何人的修改都會列出）"""
    thread = ws.resolve_thread(args.thread)
    relay = load_state(thread).get("relay") or {}
    base = relay.get("baseline") or {}
    if not base.get("commit"):
        sys.exit("此討論串的分派沒有記錄基準" + (f"：{base['error']}" if base.get("error") else "（尚未分派，或是舊版的分派）。"))
    try:
        files = snapshot.changes(ws.root, base["commit"])
    except (snapshot.SnapshotError, OSError, subprocess.SubprocessError) as e:
        sys.exit(str(e))
    if args.json:
        print(json.dumps({"baseline": base["commit"], "ref": base.get("ref"), "since": relay.get("started"),
                          "worker": relay.get("worker"), "status": relay.get("status"), "files": files},
                         ensure_ascii=False, indent=2))
        return
    print(f"分派期間的變更（{relay.get('started')} 起，{NAMES.get(relay.get('worker'), '接手方')} 接手；"
          f"期間任何人的修改都會列出）：")
    if not files:
        print("  沒有變更。")
    for f in files:
        print(f"  {snapshot.STATUS.get(f['status'], f['status'])}  {f['path']}")
    if files:
        print(f"\n查看內容：git diff {base['commit'][:12]} -- <檔案>（未追蹤的新檔不在 git diff 內）")


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
                   agora=agora_cmd())
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
        live = Live(live_path(thread, "work"), f"{NAMES[worker]}（接手）", ws.root)
        try:
            reply, session_id, meta = CALLERS[worker](prompt, relay.get("session_id"), call_opts(ws, state, worker),
                                                      True, ws.root, WORK_TIMEOUT, thread, live=live)
        except (CallError, subprocess.TimeoutExpired, FileNotFoundError) as e:
            live.close()
            exhausted = isinstance(e, CallError) and e.quota_exhausted
            log(f"呼叫失敗（額度={exhausted}）：{e}")
            add_system_note(thread, f"⚠️ {NAMES[worker]} 接手時呼叫失敗：{str(e)[:500]}")
            if getattr(e, "session_id", None):
                with thread_lock(thread, wait=120):
                    state = load_state(thread)
                    remember_session(state, worker, e.session_id, e.session_id == relay.get("external_session"))
                    save_state(thread, state)
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
        live.close()
        changed = git_status(ws.root)

        with thread_lock(thread, wait=120):
            state = load_state(thread)
            add_message(thread, state, worker, reply, via_cli=True, meta=f"接手第 {relay['rounds'] + 1} 輪, {meta}")
            state["relay"].update(session_id=session_id, rounds=relay["rounds"] + 1, updated=now_str())
            remember_session(state, worker, session_id, session_id == relay.get("external_session"))
            save_state(thread, state)
        shares.record(ws.root, worker, thread.name)
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

DEFAULT_CHECK = [sys.executable, "-m", "unittest", "discover", "-s", "tests"]


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
    quota.main([f for f, on in (("--refresh", args.refresh), ("--json", args.json)) if on])


def cmd_parties(ws: Workspace, args):
    """查看或修改此專案啟用的 AI、模型與工作分配比例"""
    split = lambda v: [canonical(p) for p in v.split(",") if p.strip()] if v else []  # noqa: E731
    enable, disable = split(args.enable), split(args.disable)
    bad = [p for p in enable + disable if p not in AI_PARTIES]
    if bad:
        sys.exit(f"未知的 AI：{bad}，可用：{list(AI_PARTIES)}")
    models_, efforts, shares_ = parse_pairs(args.model), parse_pairs(args.effort), parse_pairs(args.share)
    if enable or disable or models_ or efforts or shares_:
        guard_nested()
        try:
            settings = config.update_parties(ws.root, enable, disable, models_, efforts, shares_)
        except ValueError as e:
            sys.exit(str(e))
    else:
        settings = config.party_settings(ws.root)
    work = shares.recent_work(ws.root)
    if args.json:
        print(json.dumps({"workspace": str(ws.root), "parties": settings, "work": work,
                          "share_days": shares.SHARE_DAYS, "summarizer": ws.config().get("summarizer")},
                         ensure_ascii=False, indent=2))
        return
    total = sum(s["share"] for s in settings.values() if s["enabled"])
    done = sum(work.get(p, 0) for p, s in settings.items() if s["enabled"])
    print(f"工作區：{ws.root}")
    for p, s in settings.items():
        detail = "、".join(v for v in (s["model"], s["effort"]) if v) or "CLI 預設"
        line = f"  {'☑' if s['enabled'] else '☐'} {NAMES[p]:<12} {detail}"
        if s["enabled"]:
            target = f"{int(s['share'] / total * 100 + 0.5)}%" if total else "-"
            actual = f"{int(work.get(p, 0) / done * 100 + 0.5)}%" if done else "-"
            line += f"｜分配比例 {s['share']}（目標 {target}，近 {shares.SHARE_DAYS:g} 天 {work.get(p, 0)} 輪 {actual}）"
        print(line)


def cmd_wait(ws: Workspace, args):
    """接上背景進行中的討論，顯示它的輸出直到結束"""
    thread = ws.resolve_thread(args.thread)
    if jobs.active(thread):
        sys.exit(jobs.follow(thread))
    job = jobs.read(thread)
    if not job:
        print("此討論串沒有背景討論紀錄。")
        return
    print(f"上一次的背景討論（{job.get('kind')}）已結束：{job.get('status')}（{job.get('finished') or job.get('updated')}）")
    if args.output and (thread / "job.log").exists():
        print((thread / "job.log").read_text(encoding="utf-8"))


def cmd_stop(ws: Workspace, args):
    """停止背景進行中的討論（已收到的回覆都已寫入逐字稿）"""
    guard_nested()
    thread = ws.resolve_thread(args.thread)
    job = jobs.active(thread)
    if not job:
        print("此討論串沒有進行中的討論。分派的工作請用 recall。")
        return
    if job.get("pid"):
        jobs.kill_tree(job["pid"])
    jobs.update(thread, status="stopped", current=None, finished=now_str())
    (thread / ".lock").unlink(missing_ok=True)
    live_path(thread, "discussion").unlink(missing_ok=True)   # 被終止的程序來不及清掉進行中的內容
    current = job.get("current")
    add_system_note(thread, "⏹ 已停止背景討論" + (f"（當時在等 {NAMES[current]} 回覆，未完成的回覆不寫入逐字稿）" if current else "") + "。")
    print("已停止。")


def activity(ws: Workspace) -> List[dict]:
    """工作區內所有進行中的討論與分派"""
    items = []
    threads = sorted(p for p in ws.threads.glob("*") if (p / "state.json").exists()) if ws.threads.exists() else []
    for thread in threads:
        state = load_state(thread)
        job = jobs.active(thread)
        if job:
            live = live_path(thread, "discussion")
            items.append({"thread": thread.name, "topic": state["topic"], "type": "discussion", "kind": job.get("kind"),
                          "current": job.get("current"), "since": job.get("since"), "started": job.get("started"),
                          "pid": job.get("pid"), "live": str(live) if live.exists() else None})
        relay = state.get("relay") or {}
        if relay.get("status") in ("starting", "running", "waiting"):
            if pid_alive(relay.get("pid")):
                live = live_path(thread, "work")
                items.append({"thread": thread.name, "topic": state["topic"], "type": "relay",
                              "worker": relay["worker"], "status": relay["status"], "rounds": relay.get("rounds", 0),
                              "waiting_until": relay.get("waiting_until"), "started": relay.get("started"),
                              "pid": relay.get("pid"), "live": str(live) if live.exists() else None})
            else:
                update_relay(thread, status="interrupted")
    return items


def cmd_activity(ws: Workspace, args):
    items = activity(ws)
    if args.json:
        print(json.dumps(items, ensure_ascii=False, indent=2))
        return
    if not items:
        print("目前沒有進行中的討論或分派。")
    for it in items:
        if it["type"] == "discussion":
            who = f"等待 {NAMES[it['current']]} 回覆（{int((time.time() - it['since']) / 60)} 分鐘）" \
                if it.get("current") and it.get("since") else "啟動中"
            print(f"💬 {it['thread']}｜{it['topic']}｜{it['kind']}｜{who}")
        else:
            wait = f"，等到 {quota.fmt_time(it['waiting_until'])}" if it.get("waiting_until") else ""
            print(f"🛠 {it['thread']}｜{it['topic']}｜{NAMES[it['worker']]} 接手 {it['status']}{wait}｜已跑 {it['rounds']} 輪")


def cmd_keep(ws: Workspace, args):
    """標記討論串為保留（prune 不會清理），--off 取消"""
    thread = ws.resolve_thread(args.thread)
    with thread_lock(thread):
        state = load_state(thread)
        state["keep"] = not args.off
        save_state(thread, state)
    print(f"{'已取消保留' if args.off else '已標記保留'}：{thread.name}（{state['topic']}）")


def cmd_prune(ws: Workspace, args):
    """清理不再需要的討論串（預設只列出，--yes 才刪除）；條件見 prune.py"""
    if args.thread:
        threads, days = [ws.resolve_thread(t) for t in args.thread], None
    else:
        threads = sorted(p for p in ws.threads.glob("*") if (p / "state.json").exists()) if ws.threads.exists() else []
        days = args.days
    candidates, kept = prune.scan(threads, days)
    result = {"candidates": candidates, "kept": kept, "deleted": [], "errors": [], "dry_run": not args.yes}
    if args.yes:
        guard_nested()
        for info in candidates:
            out = prune.delete_thread(ws.threads / info["id"], info, not args.keep_sessions)
            result["deleted"].append({**info, "removed": out["done"]})
            result["errors"] += out["errors"]
        prune.compact_work_log(ws.root, shares.SHARE_DAYS)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    def describe(info):
        sessions = "、".join(sorted({NAMES[s["party"]] for s in info["sessions"]})) or "無"
        return (f"{info['id']}｜{info['topic']}｜{info['messages']} 則｜閒置 {info['idle_days']:g} 天｜"
                f"{info['bytes'] / 1024:.0f} KB｜AI 對話紀錄：{sessions}")
    scope = "指定的討論串" if args.thread else f"超過 {args.days:g} 天沒有活動的討論串"
    if not candidates:
        print(f"沒有可清理的討論串（條件：{scope}、未標記保留、沒有未完成的分派、未在使用中）。")
    elif args.yes:
        print(f"已刪除 {len(candidates)} 個討論串：")
        for info in result["deleted"]:
            print(f"  • {describe(info)}")
            for line in info["removed"]:
                print(f"      - {line}")
    else:
        print(f"以下 {len(candidates)} 個討論串可以清理（{scope}）：")
        for info in candidates:
            print(f"  • {describe(info)}")
        extra = "" if args.keep_sessions else "，並刪除 Agora 為它們在各 CLI 開的對話紀錄"
        print(f"\n確認後加上 --yes 刪除{extra}。要留下某一串可先執行 agora keep <id>。")
    for e in result["errors"]:
        print(f"⚠️ {e}")
    if kept and args.verbose:
        print("\n保留：")
        for info in kept:
            print(f"  • {info['id']}｜{info['topic']}｜{info['reason']}")
    elif kept:
        print(f"（另有 {len(kept)} 個討論串保留，加 --verbose 查看原因）")


def cmd_models(ws: Workspace, args):
    """各 AI 可選的模型與推理強度"""
    data = models.catalog(refresh=args.refresh)
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return
    for p in AI_PARTIES:
        c = data.get(p) or {}
        default = "、".join(v for v in (c.get("default") or {}).values() if v) or "未知"
        print(f"{NAMES[p]}（預設：{default}）")
        for m in c.get("models", []):
            print(f"  {m['id']:<28} {m['label']}")
        if c.get("efforts"):
            print(f"  推理強度：{' / '.join(c['efforts'])}")
        if c.get("error"):
            print(f"  ⚠️ {c['error']}")


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


SKILL_ROOT = "D:/github/agora"   # skills 範本裡的 Agora 位置，install 時換成實際位置或 agora 指令


def cmd_install(ws: Workspace, args):
    """把 Agora 裝進工作區：啟用中各 AI 的使用說明、額度 hook、Antigravity 指令權限、.gitignore、預設設定"""
    import shutil
    root = AGORA_ROOT_DIR
    hook = f"python {quote_path((root / 'agoralib' / 'quota_hook.py').as_posix())}"
    legacy = f"python {quote_path(root.as_posix() + '/agora.py')}"
    cmd = agora_cmd() if agora_cmd() == "agora" else legacy   # skills 與權限裡給 AI 用的指令
    done = []

    config_file = config.config_path(ws.root)
    if not config_file.exists():
        config.save(ws.root, {"check": DEFAULT_CHECK, "parties": {p: {"enabled": True} for p in AI_PARTIES}})
        done.append(f"預設設定 → {config_file}（用 agora parties 或 VSCode 擴充套件選擇參與的 AI）")
    enabled = ws.enabled()

    for agent, dest in (("claude", ws.root / ".claude" / "skills"), ("antigravity", ws.root / ".agents" / "skills")):
        if agent not in enabled:
            continue
        for skill in ("agora", "relay"):
            target = dest / skill
            target.mkdir(parents=True, exist_ok=True)
            content = (root / "skills" / agent / skill / "SKILL.md").read_text(encoding="utf-8")
            content = content.replace(f"python {SKILL_ROOT}/agora.py", cmd).replace(SKILL_ROOT, root.as_posix())
            (target / "SKILL.md").write_text(content, encoding="utf-8")
        done.append(f"{agent} skills → {dest}")

    # Codex（以及同樣會讀 AGENTS.md 的 Antigravity）：以標記區段寫入，重裝時整段替換
    created_agents_md = created_hooks = False
    if "codex" in enabled:
        agents_md = ws.root / "AGENTS.md"
        created_agents_md = not agents_md.exists()
        section = (root / "skills" / "codex" / "AGENTS.md").read_text(encoding="utf-8").strip()
        section = section.replace(f"python {SKILL_ROOT}/agora.py", cmd).replace(SKILL_ROOT, root.as_posix())
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
    if "claude" in enabled:
        _merge_json(ws.root / ".claude" / "settings.local.json", claude_hooks)
        done.append("Claude hooks → .claude/settings.local.json（個人設定，不進 git）")

    def agy_hooks(data):
        data["agora-quota"] = {"PreInvocation": [{"type": "command", "command": f"{hook} agy-pre", "timeout": 15}]}

    # Antigravity CLI 的權限是全域、完整比對：只開放接手方需要的固定指令。
    # 寫檔不放在這裡（否則討論時也能改檔），接手工作時以 --mode accept-edits 開放；舊版加過的寫入規則一併移除。
    def agy_permissions(data):
        allow = data.setdefault("permissions", {}).setdefault("allow", [])
        old = {f"write_file({ws.root})", f"write_file({ws.root.as_posix()})"}
        allow[:] = [rule for rule in allow if rule not in old]
        cmds = list(dict.fromkeys([cmd, legacy]))   # 舊的路徑指令也保留，已裝過的 skills 仍可用
        for rule in (*[f"command({c} {sub})" for c in cmds for sub in ("quota", "check")], "command(git status)",
                     "command(git diff)", "command(git log)",
                     f"read_file({AGORA_ROOT_DIR})", f"read_file({AGORA_ROOT_DIR.as_posix()})"):
            if rule not in allow:
                allow.append(rule)
    if "antigravity" in enabled:
        created_hooks = not (ws.root / ".agents" / "hooks.json").exists()
        _merge_json(ws.root / ".agents" / "hooks.json", agy_hooks)
        done.append("Antigravity hook → .agents/hooks.json")
        _merge_json(Path.home() / ".gemini" / "antigravity-cli" / "settings.json", agy_permissions)
        done.append("Antigravity 權限 → ~/.gemini/antigravity-cli/settings.json")

    # Agora 建立的檔案寫進 .git/info/exclude（不改會被 commit 的 .gitignore）；
    # AGENTS.md、.agents/hooks.json 可能是使用者自己的檔案，只有這次由 install 建立時才排除
    owned = [".agora/"]
    if "claude" in enabled:
        owned += [".claude/skills/agora/", ".claude/skills/relay/", ".claude/settings.local.json"]
    if "antigravity" in enabled:
        owned += [".agents/skills/agora/", ".agents/skills/relay/"] + ([".agents/hooks.json"] if created_hooks else [])
    if "codex" in enabled and created_agents_md:
        owned.append("AGENTS.md")
    excluded, tracked = git_exclude(ws.root, owned)
    if excluded is None:
        done.append("不是 git repo，沒有設定 git 排除")
    else:
        done.append(f"git 排除（{excluded}）：{'、'.join(owned)}")

    print(f"已將 Agora 安裝到 {ws.root}（參與的 AI：{'、'.join(NAMES[p] for p in enabled)}）：")
    for d in done:
        print(f"  • {d}")
    if tracked:
        print("⚠️ 以下 Agora 的檔案已被 git 追蹤，排除設定對它們無效；不想 commit 的話可執行 "
              "git rm -r --cached <路徑>（只取消追蹤，檔案留在磁碟）：")
        for t in tracked:
            print(f"    {t}")
    if "claude" in enabled:
        print("Claude Code 需重新載入視窗（或開啟 /hooks）才會套用新的 hook。")


EXCLUDE_BEGIN, EXCLUDE_END = "# agora:begin（由 agora install 管理）", "# agora:end"


def git_exclude(root: Path, paths: List[str]):
    """把 paths（相對於工作區）加進 git 的 info/exclude 的 Agora 區段，保留區段內之前加過的項目。
    回傳 (exclude 檔路徑或 None（不是 git repo）, 已被 git 追蹤的路徑)"""
    def git(*args):
        proc = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=30)
        return proc.stdout.strip() if proc.returncode == 0 else None
    try:
        if git("rev-parse", "--is-inside-work-tree") != "true":
            return None, []
        exclude = Path(git("rev-parse", "--git-path", "info/exclude"))
        prefix = git("rev-parse", "--show-prefix") or ""
    except (OSError, subprocess.SubprocessError, TypeError):
        return None, []
    exclude = exclude if exclude.is_absolute() else root / exclude
    patterns = [f"/{prefix}{p}" for p in paths]   # 以 repo 根目錄為準、開頭加 / 只比對這個位置
    text = exclude.read_bytes().decode("utf-8") if exclude.exists() else ""
    eol = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines()
    if EXCLUDE_BEGIN in lines and EXCLUDE_END in lines[lines.index(EXCLUDE_BEGIN):]:
        start = lines.index(EXCLUDE_BEGIN)
        end = lines.index(EXCLUDE_END, start)
        old, lines = lines[start + 1:end], lines[:start] + lines[end + 1:]
    else:
        start, old = len(lines), []
    block = [EXCLUDE_BEGIN] + list(dict.fromkeys(old + patterns)) + [EXCLUDE_END]
    lines[start:start] = block
    exclude.parent.mkdir(parents=True, exist_ok=True)
    new = eol.join(lines) + eol
    if new != text:
        with open(exclude, "w", encoding="utf-8", newline="") as f:
            f.write(new)
    tracked = (git("ls-files", "--", *paths) or "").splitlines()
    return exclude, tracked


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
    parser.add_argument("-v", "--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--workspace", help="工作區（預設：目前資料夾的 git 根目錄）")
    sub = parser.add_subparsers(dest="command", required=True)
    who = list(AI_PARTIES) + [HUMAN, "richard"]

    p = sub.add_parser("new", help="建立討論串，輸出 ID")
    p.add_argument("topic")
    p.add_argument("--parties", help="參與的 AI，逗號分隔（預設：此專案啟用的全部 AI）")
    p.add_argument("--model", action="append", help="此討論串的模型，例如 --model claude=sonnet（預設依專案設定）")
    p.add_argument("--effort", action="append", help="此討論串的推理強度，例如 --effort codex=high")
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
        p.add_argument("--from-summary", action="store_true", help="用討論串總結（summarize）的工作單分派")
        p.add_argument("--yes", action="store_true", help="總結可能已過期時仍確認分派")
        add_message_args(p)
        p.set_defaults(func=cmd_assign)

    p = sub.add_parser("summarize", help="請指定的一方整理討論：共識、取捨、異議與工作單（summary.md）")
    p.add_argument("thread")
    p.add_argument("--by", choices=AI_PARTIES, help="總結方（預設：.agora/config.json 的 summarizer）")
    p.set_defaults(func=cmd_summarize)

    for name in ("status", "relay-status"):
        p = sub.add_parser(name, help="查看分派狀態與最新進度")
        p.add_argument("thread")
        p.add_argument("--wait", type=int, help="最多等待幾秒，直到接手程序結束")
        p.set_defaults(func=cmd_status)

    p = sub.add_parser("changes", help="分派期間的變更（相對於分派當下的工作區快照）")
    p.add_argument("thread")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_changes)

    p = sub.add_parser("recall", help="請接手方在下一個檢查點暫停")
    p.add_argument("thread")
    p.set_defaults(func=cmd_recall)

    p = sub.add_parser("check", help="工作區驗證（語法檢查＋.agora/config.json 的 check 指令）")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("quota", help="三方剩餘額度")
    p.add_argument("--refresh", action="store_true", help="忽略快取重新查詢")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_quota)

    p = sub.add_parser("parties", help="查看或修改此專案參與的 AI、模型與推理強度")
    p.add_argument("--enable", help="啟用的 AI，逗號分隔")
    p.add_argument("--disable", help="停用的 AI，逗號分隔")
    p.add_argument("--model", action="append", help="例如 --model codex=gpt-6.1-sol；值為 default 表示改回 CLI 預設")
    p.add_argument("--effort", action="append", help="例如 --effort claude=high；值為 default 表示改回 CLI 預設")
    p.add_argument("--share", action="append",
                   help="自動分派的工作比例，例如 --share claude=5 --share codex=3；0 表示不自動分派給它，default 改回 1")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_parties)

    p = sub.add_parser("models", help="各 AI 可選的模型與推理強度")
    p.add_argument("--refresh", action="store_true", help="忽略快取重新查詢")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_models)

    p = sub.add_parser("wait", help="接上背景進行中的討論，顯示輸出直到結束")
    p.add_argument("thread")
    p.add_argument("--output", action="store_true", help="討論已結束時，顯示它的完整輸出")
    p.set_defaults(func=cmd_wait)

    p = sub.add_parser("stop", help="停止背景進行中的討論")
    p.add_argument("thread")
    p.set_defaults(func=cmd_stop)

    p = sub.add_parser("activity", help="列出進行中的討論與分派")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_activity)

    p = sub.add_parser("prune", help="清理不再需要的討論串（預設只列出，--yes 才刪除）")
    p.add_argument("--days", type=float, default=30, help="超過幾天沒有活動才清理（預設 30）")
    p.add_argument("--thread", action="append", help="只清理指定的討論串（不看天數，其餘條件照樣檢查）")
    p.add_argument("--yes", action="store_true", help="確認刪除")
    p.add_argument("--keep-sessions", action="store_true", help="只刪討論串，保留各 CLI 的對話紀錄")
    p.add_argument("--verbose", action="store_true", help="列出保留的討論串與原因")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_prune)

    p = sub.add_parser("keep", help="標記討論串為保留，prune 不會清理（--off 取消）")
    p.add_argument("thread")
    p.add_argument("--off", action="store_true")
    p.set_defaults(func=cmd_keep)

    p = sub.add_parser("install", help="把 Agora 裝進工作區（啟用中 AI 的 skills、hooks、權限、.gitignore）")
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
    job_thread = os.getenv(jobs.JOB_ENV)
    if job_thread and args.command in ("send", "reply", "auto", "summarize"):
        jobs.run(Path(job_thread), lambda: args.func(ws, args))
    args.func(ws, args)
