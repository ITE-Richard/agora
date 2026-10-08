"""
參與方：Claude Code、Antigravity、Codex 的非互動 CLI 呼叫，以及人類（使用者）。

每個 AI 參與方提供 call(prompt, session_id, opts, work, workspace) -> (回覆, session_id, 附註)：
- 討論模式（work=False）：唯讀，不改檔。
- 工作模式（work=True）：可在工作區內改檔，終端指令受限。
失敗時丟出 CallError，標記是否為額度用盡、是否因權限被拒（可接續原對話重試）。
"""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

from agoralib import quota

AGORA_ROOT = Path(__file__).resolve().parents[1]
AGORA_CMD = f"python {AGORA_ROOT.as_posix()}/agora.py"   # 給接手方執行的固定指令（權限採完整比對）
NESTED_ENV = "AGORA_INVOKED"
QUOTA_ERROR = re.compile(r"quota|exhaust|rate.?limit|usage limit|429|resource_exhausted|credits", re.I)

HUMAN = "human"
AI_PARTIES = ("claude", "antigravity", "codex")
NAMES = {"claude": "Claude Code", "antigravity": "Antigravity", "codex": "Codex",
         HUMAN: os.getenv("AGORA_HUMAN_NAME", "Richard")}
ALIASES = {"richard": HUMAN, "user": HUMAN, "agy": "antigravity", "gemini": "antigravity", "openai": "codex"}


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


def _run(cmd: list, workspace: Path, stdin_text: Optional[str], timeout: int) -> Tuple[list, str]:
    env = {**os.environ, NESTED_ENV: "1"}
    env.pop("AGORA_JOB", None)   # AI 執行的 agora 指令（quota、check）不是背景討論程序
    proc = subprocess.run(cmd, cwd=workspace, input=stdin_text if stdin_text is not None else "",
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout + 60, env=env)
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
            detail = f"(exit={proc.returncode})\n{proc.stdout[-1500:]}\n{proc.stderr[-1500:]}"
            raise CallError(f"無法解析輸出 {detail}", bool(QUOTA_ERROR.search(detail)))
    return events, proc.stderr


# ───────────────────────── Antigravity ─────────────────────────

def find_agy() -> str:
    if os.getenv("AGY_PATH"):
        return os.environ["AGY_PATH"]
    name = "agy.exe" if sys.platform.startswith("win") else "agy"
    default = Path.home() / ".gemini" / "bin" / name
    if default.exists():
        return str(default)
    return shutil.which("agy") or str(default)


def call_antigravity(prompt: str, session_id: Optional[str], opts: dict, work: bool, workspace: Path,
                     timeout: int, scratch: Path) -> Tuple[str, str, str]:
    overflow = None
    try:
        if len(prompt) > 20000:  # Windows 命令列上限約 32k；agy 只能從參數收訊息
            overflow = scratch / f"long_message_{os.getpid()}.md"
            overflow.write_text(prompt, encoding="utf-8")
            prompt = f"這則訊息較長，完整內容在 {overflow}，請先讀取該檔再照內容回覆。"
        cmd = [find_agy(), "-p", prompt, "--output-format", "json", "--print-timeout", f"{timeout}s"]
        if session_id:
            cmd += ["--conversation", session_id]
        if opts.get("model"):
            cmd += ["--model", opts["model"]]
        if opts.get("effort"):
            cmd += ["--effort", opts["effort"]]
        events, stderr = _run(cmd, workspace, None, timeout)
        result = events[-1]
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

def call_claude(prompt: str, session_id: Optional[str], opts: dict, work: bool, workspace: Path,
                timeout: int, scratch: Path) -> Tuple[str, str, str]:
    cmd = [quota.find_claude(), "-p", "--output-format", "stream-json", "--verbose"]
    if work:
        # 可改檔；Bash 只開放 Agora 的固定指令與唯讀 git，其餘在非互動模式下會被拒絕
        cmd += ["--permission-mode", "acceptEdits",
                "--allowedTools", f"Bash({AGORA_CMD} quota*)", f"Bash({AGORA_CMD} check*)",
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
    events, stderr = _run(cmd, workspace, prompt, timeout)
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

def call_codex(prompt: str, session_id: Optional[str], opts: dict, work: bool, workspace: Path,
               timeout: int, scratch: Path) -> Tuple[str, str, str]:
    # 討論唯讀；工作模式只能寫工作區（Codex 沙箱），不碰工作區外
    sandbox = "workspace-write" if work else "read-only"
    cmd = [quota.find_codex(), "exec", "--json", "-s", sandbox, "-C", str(workspace), "--skip-git-repo-check"]
    if opts.get("model"):
        cmd += ["-m", opts["model"]]
    if opts.get("effort"):
        cmd += ["-c", f'model_reasoning_effort="{opts["effort"]}"']
    cmd += ["resume", session_id, "-"] if session_id else ["-"]
    events, stderr = _run(cmd, workspace, prompt, timeout)
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
