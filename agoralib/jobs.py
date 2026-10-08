"""
背景執行：討論（send / reply / auto）與分派工作都在獨立的背景程序執行，
呼叫者（使用者或某個 AI 的終端指令）只負責顯示進度。呼叫者被終止（例如關掉 VSCode 視窗）時，
背景程序不受影響，結果照樣寫進討論串。

Windows 上關閉視窗會以「整棵程序樹」終止呼叫者，直接啟動的子程序（即使 DETACHED）也會被一併終止；
所以用兩段式啟動：中間程序啟動背景程序後立即結束，背景程序就不再是呼叫者的後代。

每個討論串同時只有一個討論工作，狀態在 <討論串>/job.json，輸出在 job.log / job.err：
  {"kind": "send", "pid": 1234, "status": "running", "current": "codex", "since": ..., "exit": null}
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

JOB_ENV = "AGORA_JOB"          # 背景程序內設為討論串路徑
RUNNING = ("starting", "running")
FLAGS = 0
if sys.platform.startswith("win"):
    FLAGS = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
BREAKAWAY = 0x01000000          # CREATE_BREAKAWAY_FROM_JOB：呼叫者若在 Windows job 內，也一併脫離


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


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


def kill_tree(pid: int):
    if sys.platform.startswith("win"):
        subprocess.run(["taskkill", "/pid", str(pid), "/T", "/F"], capture_output=True)
    else:
        try:
            os.killpg(pid, 15)
        except OSError:
            pass


# ───────────────────────── 兩段式啟動 ─────────────────────────

def spawn(argv: list, cwd: Path, stdout: Path, stderr: Path, append: bool = False, env: Optional[dict] = None) -> int:
    """在背景啟動 argv，回傳 PID；背景程序不屬於呼叫者的程序樹"""
    pid_file = stdout.with_name(f".spawn-{os.getpid()}-{time.time_ns()}.pid")
    spec = {"argv": argv, "cwd": str(cwd), "stdout": str(stdout), "stderr": str(stderr), "append": append,
            "env": env or {}, "pid_file": str(pid_file)}
    extra = {"creationflags": FLAGS} if sys.platform.startswith("win") else {}
    subprocess.run([sys.executable, str(Path(__file__).resolve()), "launch", json.dumps(spec)],
                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, timeout=60, check=True, **extra)
    pid = int(pid_file.read_text(encoding="utf-8"))
    pid_file.unlink(missing_ok=True)
    return pid


def _launch(spec: dict):
    mode = "a" if spec["append"] else "w"
    out = open(spec["stdout"], mode, encoding="utf-8")
    err = out if spec["stderr"] == spec["stdout"] else open(spec["stderr"], mode, encoding="utf-8")
    kwargs = dict(cwd=spec["cwd"], stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                  env={**os.environ, "PYTHONUNBUFFERED": "1", **spec["env"]})
    if not sys.platform.startswith("win"):
        proc = subprocess.Popen(spec["argv"], start_new_session=True, **kwargs)
    else:
        try:
            proc = subprocess.Popen(spec["argv"], creationflags=FLAGS | BREAKAWAY, **kwargs)
        except OSError:   # 所在的 job 不允許脫離
            proc = subprocess.Popen(spec["argv"], creationflags=FLAGS, **kwargs)
    Path(spec["pid_file"]).write_text(str(proc.pid), encoding="utf-8")


# ───────────────────────── 討論工作 ─────────────────────────

def job_path(thread: Path) -> Path:
    return thread / "job.json"


def read(thread: Path) -> dict:
    try:
        return json.loads(job_path(thread).read_text(encoding="utf-8"))
    except Exception:
        return {}


def update(thread: Path, **fields) -> dict:
    job = {**read(thread), **fields, "updated": now_str()}
    tmp = thread / "job.json.tmp"
    tmp.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(job_path(thread))
    return job


def active(thread: Path) -> Optional[dict]:
    """進行中的討論工作；程序已不存在但狀態還是 running 的（被強制終止）改記為 interrupted"""
    job = read(thread)
    if job.get("status") not in RUNNING:
        return None
    if job.get("status") == "starting" and time.time() - job.get("started_ts", 0) < 60:
        return job
    if pid_alive(job.get("pid")):
        return job
    update(thread, status="interrupted", current=None, finished=now_str())
    return None


def start(thread: Path, kind: str, argv: list, cwd: Path) -> dict:
    update(thread, kind=kind, status="starting", pid=None, current=None, since=None, exit=None,
           started=now_str(), started_ts=time.time(), finished=None)
    try:   # PID 由背景程序自己寫入（run），避免和它同時改 job.json
        spawn(argv, cwd, thread / "job.log", thread / "job.err", env={JOB_ENV: str(thread)})
    except Exception as e:
        update(thread, status="failed", error=f"無法啟動背景程序：{e}", finished=now_str())
        raise
    return read(thread)


def run(thread: Path, func: Callable[[], None]):
    """在背景程序內執行討論，結束時寫入狀態與結束碼"""
    update(thread, status="running", pid=os.getpid())
    code = 0
    try:
        func()
    except SystemExit as e:
        if isinstance(e.code, str):
            print(e.code, file=sys.stderr)
            code = 1
        else:
            code = e.code or 0
    except BaseException:
        import traceback
        traceback.print_exc()
        code = 1
    finally:
        update(thread, status="done" if code == 0 else "failed", exit=code, current=None, finished=now_str())
    sys.exit(code)


def set_current(party: Optional[str]):
    """背景程序內：記下目前正在等誰回覆"""
    thread = os.getenv(JOB_ENV)
    if thread:
        update(Path(thread), current=party, since=time.time() if party else None)


def follow(thread: Path, from_start: bool = True, poll: float = 0.5) -> int:
    """把背景討論的輸出轉到目前的終端，直到它結束；回傳它的結束碼"""
    streams = [(thread / "job.log", sys.stdout), (thread / "job.err", sys.stderr)]
    offsets = {}
    for path, _ in streams:
        offsets[path] = 0 if from_start or not path.exists() else path.stat().st_size

    def pump():
        for path, out in streams:
            if not path.exists():
                continue
            with open(path, "rb") as f:
                f.seek(offsets[path])
                data = f.read()
            if data:
                offsets[path] += len(data)
                out.write(data.decode("utf-8", errors="replace"))
                out.flush()

    checks = 0
    while True:
        pump()
        if read(thread).get("status") not in RUNNING:
            break
        checks += 1
        if checks % 10 == 0 and not active(thread):   # 偶爾確認程序還在（被強制結束時狀態不會更新）
            break
        time.sleep(poll)
    pump()
    job = read(thread)
    if job.get("status") == "interrupted":
        print("⚠️ 背景討論程序已中斷（可能被強制結束），回覆前的內容都已寫入逐字稿。", file=sys.stderr)
        return 1
    if job.get("status") == "stopped":
        print("已停止。", file=sys.stderr)
        return 1
    return int(job.get("exit") or 0)


if __name__ == "__main__" and len(sys.argv) == 3 and sys.argv[1] == "launch":
    _launch(json.loads(sys.argv[2]))
