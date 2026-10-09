"""
分派基準：assign 開始時替工作區拍一份快照，之後比對「分派期間的變更」。

- 用暫時的 index 檔（GIT_INDEX_FILE）把目前的工作區（含未提交的修改與未追蹤檔）寫成 tree，
  再包成 commit 存在 refs/agora/<討論串>，避免被 git gc 回收；使用者的 index、工作區、分支都不受影響。
  GIT_INDEX_FILE 會傳給快照過程中的每一個 git 指令。
- 被 .gitignore 的檔案不進快照；.env、金鑰等機密檔即使已被 git 追蹤也一律排除（SECRET_PATTERNS）。
- 比對時同樣替目前的工作區拍一份快照（不建 ref），再用 git diff 比較兩個 tree。
  期間任何人（接手方或使用者）的修改都會列出，所以稱為「分派期間的變更」，不代表都是接手方做的。
"""

import os
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, List, Optional

REF_PREFIX = "refs/agora/"
SECRET_PATTERNS = (".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa*", "id_ed25519*", ".agora/**")
IDENTITY = {"GIT_AUTHOR_NAME": "Agora", "GIT_AUTHOR_EMAIL": "agora@localhost",
            "GIT_COMMITTER_NAME": "Agora", "GIT_COMMITTER_EMAIL": "agora@localhost"}
STATUS = {"A": "新增", "M": "修改", "D": "刪除", "T": "類型變更"}


class SnapshotError(RuntimeError):
    pass


def _run(root: Path, args, env: Optional[dict]) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=300, env={**os.environ, **(env or {})})


def _git(root: Path, *args: str, env: Optional[dict] = None, check: bool = True) -> str:
    proc = _run(root, args, env)
    if check and proc.returncode != 0:
        raise SnapshotError(f"git {' '.join(args[:2])} 失敗：{(proc.stderr or proc.stdout).strip()[:300]}")
    return proc.stdout.strip()


def _ok(root: Path, *args: str) -> bool:
    return _run(root, args, None).returncode == 0


def is_repo(root: Path) -> bool:
    try:
        return _git(root, "rev-parse", "--is-inside-work-tree", check=False) == "true"
    except (OSError, subprocess.SubprocessError):
        return False


def secret_pathspecs() -> List[str]:
    return [f":(exclude,glob)**/{p}" for p in SECRET_PATTERNS]


def _tree(root: Path) -> str:
    """目前工作區（含未追蹤檔、不含被忽略檔與機密檔）的 tree；不碰使用者的 index"""
    with tempfile.TemporaryDirectory(prefix="agora-index-") as tmp:
        env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
        if _git(root, "rev-parse", "--verify", "-q", "HEAD", check=False):
            _git(root, "read-tree", "HEAD", env=env)
        _git(root, "add", "-A", "--", ".", *secret_pathspecs(), env=env)
        # read-tree 帶進來的已追蹤機密檔，也從快照移除
        _git(root, "rm", "-r", "-q", "--cached", "--ignore-unmatch", "--",
             *[f":(glob)**/{p}" for p in SECRET_PATTERNS], env=env)
        return _git(root, "write-tree", env=env)


def create(root: Path, name: str) -> Dict[str, str]:
    """拍快照並存在 refs/agora/<name>，回傳 {"commit", "ref"}"""
    if not is_repo(root):
        raise SnapshotError("工作區不是 git repo，無法記錄分派基準。")
    tree = _tree(root)
    head = _git(root, "rev-parse", "--verify", "-q", "HEAD", check=False)
    commit = _git(root, "commit-tree", tree, *(["-p", head] if head else []), "-m", f"agora baseline {name}",
                  env=IDENTITY)
    ref = REF_PREFIX + name
    _git(root, "update-ref", ref, commit)
    return {"commit": commit, "ref": ref}


def changes(root: Path, baseline: str) -> List[Dict[str, str]]:
    """相對於基準有變動的檔案：[{"path"（相對於工作區）, "status"}]，status 為 A / M / D / T"""
    if not is_repo(root):
        raise SnapshotError("工作區不是 git repo。")
    if not _ok(root, "cat-file", "-e", f"{baseline}^{{commit}}"):
        raise SnapshotError(f"找不到分派基準 {baseline[:12]}（可能已被刪除）。")
    # --relative：工作區是 repo 的子資料夾時，只列出工作區內的檔案，路徑相對於工作區
    out = _git(root, "-c", "core.quotepath=false", "diff", "--name-status", "--no-renames", "--relative", "-z",
               f"{baseline}^{{tree}}", _tree(root))
    parts = [p for p in out.split("\0") if p]
    return [{"status": parts[i][0], "path": parts[i + 1]} for i in range(0, len(parts) - 1, 2)]


def delete(root: Path, name: str):
    """刪除 refs/agora/<name>（清理討論串時）；不存在或不是 git repo 時略過"""
    ref = REF_PREFIX + name
    if is_repo(root) and _git(root, "rev-parse", "--verify", "-q", ref, check=False):
        _git(root, "update-ref", "-d", ref)
