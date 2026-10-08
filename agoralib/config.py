"""
工作區設定：<工作區>/.agora/config.json

{
  "check": ["python", "-m", "unittest", "discover", "-s", "tests"],
  "parties": {
    "claude":      {"enabled": true,  "model": "opus", "effort": "high", "share": 2},
    "antigravity": {"enabled": false},
    "codex":       {"enabled": true,  "model": "gpt-6.1-sol", "share": 1}
  }
}

parties 沒寫到的 AI 視為啟用、模型與推理強度沿用各 CLI 自己的預設。
討論串可以另外指定模型（new --model），優先於這裡的設定。
share 是自動挑選接手方時的工作分配比例（預設 1，0 表示不自動分派給它），見 shares.py。
"""

import json
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from agoralib.parties import AI_PARTIES

FIELDS = ("model", "effort")


def config_path(root: Path) -> Path:
    return root / ".agora" / "config.json"


def load(root: Path) -> dict:
    try:
        return json.loads(config_path(root).read_text(encoding="utf-8"))
    except Exception:
        return {}


def save(root: Path, data: dict):
    path = config_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def party_settings(root: Path, data: Optional[dict] = None) -> Dict[str, dict]:
    raw = (load(root) if data is None else data).get("parties") or {}
    out = {}
    for p in AI_PARTIES:
        item = raw.get(p) or {}
        share = item.get("share", 1)
        out[p] = {"enabled": item.get("enabled", True) is not False,
                  **{f: item.get(f) or None for f in FIELDS},
                  "share": share if isinstance(share, int) and share >= 0 else 1}
    return out


def enabled(root: Path) -> List[str]:
    return [p for p, s in party_settings(root).items() if s["enabled"]]


def shares(root: Path) -> Dict[str, int]:
    return {p: s["share"] for p, s in party_settings(root).items()}


def update_parties(root: Path, enable: Iterable[str] = (), disable: Iterable[str] = (),
                   models: Optional[Dict[str, str]] = None, efforts: Optional[Dict[str, str]] = None,
                   shares: Optional[Dict[str, str]] = None) -> Dict[str, dict]:
    """更新參與方設定；模型或推理強度給空字串或 default 表示改回 CLI 預設，比例給 default 表示改回 1。
    至少要保留一個 AI。"""
    data = load(root)
    parties = data.setdefault("parties", {})
    for p, value in [(p, True) for p in enable] + [(p, False) for p in disable]:
        parties.setdefault(p, {})["enabled"] = value
    for field, values in (("model", models or {}), ("effort", efforts or {})):
        for p, value in values.items():
            item = parties.setdefault(p, {})
            if value and value != "default":
                item[field] = value
            else:
                item.pop(field, None)
    for p, value in (shares or {}).items():
        item = parties.setdefault(p, {})
        if value in ("", "default"):
            item.pop("share", None)
            continue
        try:
            share = int(value)
        except ValueError:
            share = -1
        if share < 0:
            raise ValueError(f"分配比例必須是非負整數：{p}={value}")
        item["share"] = share
    settings = party_settings(root, data)
    if not any(s["enabled"] for s in settings.values()):
        raise ValueError("至少要啟用一個 AI。")
    save(root, data)
    return settings


def find_root(start: Path) -> Optional[Path]:
    """從 start 往上找含有 .agora 的資料夾（hook 用來判斷目前專案啟用了哪些 AI）"""
    for p in [start, *start.parents]:
        if (p / ".agora").is_dir():
            return p
    return None
