"""
接手方用的固定檢查指令：語法檢查專案所有 Python 檔，再跑全部單元測試。

    python tools/agora/check.py

Antigravity 的非互動模式只允許「完全相同」的指令，所以檢查流程收在這個沒有參數的腳本裡，
權限只需開放 command(python tools/agora/check.py)。
"""

import py_compile
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_DIRS = ["config", "core", "council", "mirror", "pavilions", "web", "tests", "tools"]


def main() -> int:
    if sys.platform.startswith("win"):
        sys.stdout.reconfigure(encoding="utf-8")
    errors = []
    files = [REPO_ROOT / "main.py"] + [p for d in SOURCE_DIRS for p in (REPO_ROOT / d).rglob("*.py")]
    for f in files:
        try:
            py_compile.compile(str(f), doraise=True)
        except py_compile.PyCompileError as e:
            errors.append(str(e))
    print(f"語法檢查：{len(files)} 個檔案，{len(errors)} 個錯誤")
    for e in errors:
        print(e)

    proc = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests"], cwd=REPO_ROOT,
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    # loguru 的日誌也寫在 stderr，只留下 unittest 的結果與失敗細節
    lines = proc.stderr.splitlines()
    keep, in_failure = [], False
    for line in lines:
        if line.startswith(("ERROR:", "FAIL:")):
            in_failure = True
        if line.startswith("Ran "):
            in_failure = False
        if in_failure or line.startswith(("Ran ", "OK", "FAILED")):
            keep.append(line)
    print("單元測試：")
    print("\n".join(keep[-120:]) or proc.stderr[-3000:])
    return 1 if errors or proc.returncode else 0


if __name__ == "__main__":
    sys.exit(main())
