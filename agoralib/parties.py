"""
參與方：Claude Code、Antigravity、Codex 的非互動 CLI 呼叫，以及人類（使用者）。

每個 AI 參與方提供 call(prompt, session_id, opts, work, workspace, timeout, scratch, live=None) -> (回覆, session_id, 附註)：
- 討論模式（work=False）：唯讀，不改檔（Claude 禁用編輯工具、Codex read-only 沙箱、Antigravity 沒有寫檔權限）。
- 工作模式（work=True）：可在工作區內改檔，終端指令受限。
失敗時丟出 CallError，標記是否為額度用盡、是否因權限被拒（可接續原對話重試）。
live：進行中的回覆（Live），三個 CLI 都以串流 JSON 輸出，邊收邊更新：
- Claude：--include-partial-messages 的 text_delta；工具呼叫在 assistant 訊息的 tool_use
- Codex：沒有逐字輸出；item.started（執行指令等）與 item.completed 的 agent_message
- Antigravity：--output-format stream-json 的 step_update（text_delta、工具步驟），最後的 result 與 json 模式相同
"""

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

from agoralib import quota

AGORA_ROOT = Path(__file__).resolve().parents[1]


def quote_path(path: str) -> str:
    """指令裡的路徑含空格時加引號（例如 C:/Users/Richard Clayd/.agora/core）"""
    return f'"{path}"' if " " in path else path


LEGACY_CMD = f"python {quote_path(AGORA_ROOT.as_posix() + '/agora.py')}"
DIST_NAME = "agora-cli"
NESTED_ENV = "AGORA_INVOKED"
QUOTA_ERROR = re.compile(r"quota|exhaust|rate.?limit|usage limit|429|resource_exhausted|credits", re.I)

HUMAN = "human"
AI_PARTIES = ("claude", "antigravity", "codex")
NAMES = {"claude": "Claude Code", "antigravity": "Antigravity", "codex": "Codex",
         HUMAN: os.getenv("AGORA_HUMAN_NAME", "Richard")}
ALIASES = {"richard": HUMAN, "user": HUMAN, "agy": "antigravity", "gemini": "antigravity", "openai": "codex"}


def _installed_here(exe: Path) -> bool:
    """exe 是目前這個 Python 以 pip install -e 安裝、指向這份程式的 agora 指令"""
    import sysconfig
    from importlib import metadata
    from urllib.parse import unquote, urlparse
    scripts = {Path(p).resolve() for p in (sysconfig.get_path("scripts"),
                                            sysconfig.get_path("scripts", f"{os.name}_user")) if p}
    if exe.resolve().parent not in scripts:
        return False
    # 從 repo 目錄執行時，setuptools 留在 repo 的 egg-info 也會被找到，所以逐一檢查有安裝紀錄的那份
    for dist in metadata.distributions():
        if (dist.metadata["Name"] or "").lower() != DIST_NAME:
            continue
        try:
            direct = json.loads(dist.read_text("direct_url.json") or "{}")
        except json.JSONDecodeError:
            continue
        if not (direct.get("dir_info") or {}).get("editable"):
            continue   # 一般安裝是複製一份，不一定是這份程式的版本
        url = urlparse(direct.get("url", ""))
        path = unquote(url.path)
        if re.match(r"^/[A-Za-z]:", path):   # file:///D:/github/agora
            path = path[1:]
        if url.scheme == "file" and Path(path).resolve() == AGORA_ROOT:
            return True
    return False


_CMD = None


def agora_cmd() -> str:
    """給 AI 執行的 Agora 指令（Antigravity 的權限是完整比對，所以要固定）：
    已用 pip install -e 安裝、指向這份程式時為 agora，否則為 python <路徑>/agora.py"""
    global _CMD
    if _CMD is None:
        exe = shutil.which("agora")
        _CMD = "agora" if exe and _installed_here(Path(exe)) else LEGACY_CMD
    return _CMD


def canonical(name: str) -> str:
    name = name.strip().lower()
    return ALIASES.get(name, name)


class CallError(RuntimeError):
    def __init__(self, message: str, quota_exhausted: bool = False, denied: bool = False,
                 session_id: Optional[str] = None):
        super().__init__(message)
        self.quota_exhausted = quota_exhausted
        self.denied = denied          # 有工具呼叫被權限拒絕（agy 非互動模式會因此整輪中斷）
        self.session_id = session_id  # 失敗的那段對話，重試時可以接續


# ───────────────────────── 進行中的回覆 ─────────────────────────

class Live:
    """回覆進行中的內容：各 CLI 串流事件裡的文字與工具動作。

    每次整份覆寫 path（不追加，避免重複）；工具動作同時印到 stderr（終端與 VSCode 的進度通知會看到）。
    完成或失敗時由呼叫者 close() 刪除，正式內容只寫進逐字稿一次；被強制停止時由 stop 指令清掉。"""

    def __init__(self, path: Optional[Path], title: str, workspace: Path):
        self.path, self.title, self.workspace = path, title, workspace
        self.text, self.actions, self.written = "", [], 0.0
        self.write(force=True)

    def add_text(self, delta: str):
        if delta:
            self.text += delta
            self.write()

    def add_action(self, desc: str):
        self.actions.append(desc)
        print(f"  · {self.title}：{desc}", file=sys.stderr, flush=True)
        self.write(force=True)

    def write(self, force: bool = False):
        if not self.path or (not force and time.time() - self.written < 1):
            return
        self.written = time.time()
        body = (f"# {self.title} 回覆中…\n\n> 進行中的內容，完成後才會寫進逐字稿；中途停止的回覆不會寫入。\n\n"
                + "".join(f"- {a}\n" for a in self.actions[-30:]) + ("\n" if self.actions else "") + self.text)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(body, encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            pass   # 預覽視窗剛好在讀檔時（Windows）可能取代失敗，下一次再寫

    def close(self):
        if self.path:
            for p in (self.path, self.path.with_name(self.path.name + ".tmp")):
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    pass


TOOL_KEYS = ("file_path", "path", "AbsolutePath", "TargetFile", "notebook_path", "command", "CommandLine",
             "pattern", "Query", "query", "url", "Url", "SearchPath")


def describe_tool(name: str, params: dict, workspace: Path) -> str:
    value = next((str(params[k]) for k in TOOL_KEYS if params.get(k)), "")
    try:
        value = Path(value).resolve().relative_to(workspace.resolve()).as_posix() if Path(value).is_absolute() else value
    except (ValueError, OSError):
        pass
    value = " ".join(value.split())
    return f"{name} {value[:100] + ('…' if len(value) > 100 else '')}".strip()


def _run(cmd: list, workspace: Path, stdin_text: Optional[str], timeout: int,
         on_event: Optional[Callable[[dict], None]] = None) -> Tuple[list, str]:
    """執行 CLI，逐行解析 JSON 事件（on_event 即時處理）；逾時會終止整棵程序樹"""
    env = {**os.environ, NESTED_ENV: "1"}
    env.pop("AGORA_JOB", None)   # AI 執行的 agora 指令（quota、check）不是背景討論程序
    proc = subprocess.Popen(cmd, cwd=workspace, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", env=env)
    errors: list = []

    def feed():
        try:
            proc.stdin.write(stdin_text or "")
            proc.stdin.close()
        except OSError:
            pass

    expired = threading.Event()

    def kill():
        expired.set()
        if sys.platform.startswith("win"):
            subprocess.run(["taskkill", "/pid", str(proc.pid), "/T", "/F"], capture_output=True)
        else:
            proc.kill()

    workers = [threading.Thread(target=feed, daemon=True),
               threading.Thread(target=lambda: errors.append(proc.stderr.read()), daemon=True)]
    for w in workers:
        w.start()
    timer = threading.Timer(timeout + 60, kill)
    timer.start()
    events, lines = [], []
    try:
        for line in proc.stdout:
            lines.append(line)
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            events.append(event)
            if on_event:
                try:
                    on_event(event)
                except Exception:
                    pass   # 即時顯示只是輔助，出錯不影響回覆
        proc.wait()
    finally:
        timer.cancel()
    for w in workers:
        w.join(timeout=10)
    if expired.is_set():
        raise subprocess.TimeoutExpired(cmd, timeout + 60)
    stdout, stderr = "".join(lines), "".join(errors)
    if not events:
        try:
            events = [json.loads(stdout)]
        except json.JSONDecodeError:
            detail = f"(exit={proc.returncode})\n{stdout[-1500:]}\n{stderr[-1500:]}"
            raise CallError(f"無法解析輸出 {detail}", bool(QUOTA_ERROR.search(detail)))
    return events, stderr


# ───────────────────────── Antigravity ─────────────────────────

def find_agy() -> str:
    if os.getenv("AGY_PATH"):
        return os.environ["AGY_PATH"]
    name = "agy.exe" if sys.platform.startswith("win") else "agy"
    default = Path.home() / ".gemini" / "bin" / name
    if default.exists():
        return str(default)
    return shutil.which("agy") or str(default)


def antigravity_event(live: Live, e: dict):
    if e.get("event") != "step_update":
        return
    step = e.get("step_update") or {}
    if step.get("step_type") == "agent_response":
        live.add_text(step.get("text_delta") or "")
    elif step.get("step_type") == "tool" and step.get("state") == "ACTIVE":
        live.add_action(describe_tool(step.get("tool_name") or "tool",
                                      (step.get("tool_info") or {}).get("parameters") or {}, live.workspace))


def call_antigravity(prompt: str, session_id: Optional[str], opts: dict, work: bool, workspace: Path,
                     timeout: int, scratch: Path, live: Optional[Live] = None) -> Tuple[str, str, str]:
    overflow = None
    try:
        if len(prompt) > 20000:  # Windows 命令列上限約 32k；agy 只能從參數收訊息
            overflow = scratch / f"long_message_{os.getpid()}.md"
            overflow.write_text(prompt, encoding="utf-8")
            prompt = f"這則訊息較長，完整內容在 {overflow}，請先讀取該檔再照內容回覆。"
        cmd = [find_agy(), "-p", prompt, "--output-format", "stream-json", "--print-timeout", f"{timeout}s"]
        if work:
            # 只有接手工作時開放改檔：accept-edits 允許寫工作區內的檔案，工作區外仍會被拒絕。
            # 不在全域權限加 write_file，否則同一個工作區的討論也能改檔。
            cmd += ["--mode", "accept-edits"]
        if session_id:
            cmd += ["--conversation", session_id]
        if opts.get("model"):
            cmd += ["--model", opts["model"]]
        if opts.get("effort"):
            cmd += ["--effort", opts["effort"]]
        events, stderr = _run(cmd, workspace, None, timeout, live and (lambda e: antigravity_event(live, e)))
        result = next((e["result"] for e in reversed(events) if e.get("event") == "result" and e.get("result")),
                      events[-1])
        reply = (result.get("response") or "").strip()
        if result.get("status") != "SUCCESS" or not reply:
            detail = json.dumps(result, ensure_ascii=False)[:1500] + stderr[-500:]
            raise CallError(f"Antigravity 未產生回覆：{detail}", bool(QUOTA_ERROR.search(detail)),
                            denied=bool(result.get("denied_actions")), session_id=result.get("conversation_id"))
        meta = f"{result.get('duration_seconds', 0):.0f}s, {result.get('usage', {}).get('total_tokens', '?')} tokens"
        return reply, result.get("conversation_id") or session_id, meta
    finally:
        if overflow:
            try:
                overflow.unlink(missing_ok=True)
            except OSError:
                pass


# ───────────────────────── Claude Code ─────────────────────────

def claude_event(live: Live, e: dict):
    if e.get("type") == "stream_event":
        event = e.get("event") or {}
        delta = event.get("delta") or {}
        if event.get("type") == "content_block_delta" and delta.get("type") == "text_delta":
            live.add_text(delta.get("text") or "")
        elif event.get("type") == "message_start" and live.text and not live.text.endswith("\n\n"):
            live.add_text("\n\n")
    elif e.get("type") == "assistant":
        for block in (e.get("message") or {}).get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                live.add_action(describe_tool(block.get("name") or "tool", block.get("input") or {}, live.workspace))


def call_claude(prompt: str, session_id: Optional[str], opts: dict, work: bool, workspace: Path,
                timeout: int, scratch: Path, live: Optional[Live] = None) -> Tuple[str, str, str]:
    cmd = [quota.find_claude(), "-p", "--output-format", "stream-json", "--verbose", "--include-partial-messages"]
    if work:
        # 可改檔；Bash 只開放 Agora 的固定指令與唯讀 git，其餘在非互動模式下會被拒絕
        cmd += ["--permission-mode", "acceptEdits",
                "--allowedTools", f"Bash({agora_cmd()} quota*)", f"Bash({agora_cmd()} check*)",
                "Bash(git status*)", "Bash(git diff*)", "Bash(git log*)",
                "--disallowedTools", "Read(./.env)", "Edit(./.env)"]
    else:
        cmd += ["--disallowedTools", "Edit", "Write", "NotebookEdit"]
    if session_id:
        cmd += ["--resume", session_id]
    if opts.get("model"):
        cmd += ["--model", opts["model"]]
    if opts.get("effort"):
        cmd += ["--effort", opts["effort"]]
    events, stderr = _run(cmd, workspace, prompt, timeout, live and (lambda e: claude_event(live, e)))
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


# ───────────────────────── Codex ─────────────────────────

def codex_event(live: Live, e: dict):
    item = e.get("item") or {}
    if e.get("type") == "item.started":
        if item.get("type") == "command_execution":
            command = item.get("command") or ""
            m = re.search(r'-Command\s+"(.*)"\s*$', command, re.S)   # Windows 上包在 powershell -Command "…" 裡
            live.add_action(describe_tool("執行", {"command": m.group(1) if m else command}, live.workspace))
        elif item.get("type") not in (None, "agent_message", "reasoning"):
            live.add_action(item["type"])
    elif e.get("type") == "item.completed" and item.get("type") == "agent_message":
        live.add_text((item.get("text") or "").strip() + "\n\n")


def call_codex(prompt: str, session_id: Optional[str], opts: dict, work: bool, workspace: Path,
               timeout: int, scratch: Path, live: Optional[Live] = None) -> Tuple[str, str, str]:
    # 討論唯讀；工作模式只能寫工作區（Codex 沙箱），不碰工作區外
    sandbox = "workspace-write" if work else "read-only"
    cmd = [quota.find_codex(), "exec", "--json", "-s", sandbox, "-C", str(workspace), "--skip-git-repo-check"]
    if opts.get("model"):
        cmd += ["-m", opts["model"]]
    if opts.get("effort"):
        cmd += ["-c", f'model_reasoning_effort="{opts["effort"]}"']
    cmd += ["resume", session_id, "-"] if session_id else ["-"]
    events, stderr = _run(cmd, workspace, prompt, timeout, live and (lambda e: codex_event(live, e)))
    thread_id = next((e.get("thread_id") for e in events if e.get("type") == "thread.started"), session_id)
    messages = [e["item"].get("text", "") for e in events
                if e.get("type") == "item.completed" and (e.get("item") or {}).get("type") == "agent_message"]
    errors = [json.dumps(e, ensure_ascii=False) for e in events if e.get("type") in ("error", "turn.failed")]
    if thread_id:
        quota.update_codex_snapshot_from_session(thread_id)
    reply = (messages[-1] if messages else "").strip()
    if errors or not reply:
        detail = "；".join(errors)[:1500] + stderr[-500:]
        raise CallError(f"Codex 未產生回覆：{detail}", bool(QUOTA_ERROR.search(detail)), session_id=thread_id)
    usage = next((e.get("usage") for e in reversed(events) if e.get("type") == "turn.completed"), {}) or {}
    meta = f"{usage.get('input_tokens', '?')}+{usage.get('output_tokens', '?')} tokens"
    return reply, thread_id, meta


CALLERS: Dict[str, Callable] = {"claude": call_claude, "antigravity": call_antigravity, "codex": call_codex}
