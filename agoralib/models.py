"""
三方可選的模型與推理強度

- Claude Code：CLI 沒有列出模型的指令，提供官方別名（永遠指向該系列最新版）；預設值讀 ~/.claude/settings.json。
- Antigravity：`agy models`（模型名稱已含推理強度，例如 gemini-3.1-pro-high）。
- Codex：~/.codex/models_cache.json（Codex 自己維護的模型目錄），沒有時改跑 `codex debug models`；
  預設值讀 ~/.codex/config.toml。

結果快取在 ~/.agora/models.json（12 小時）。
"""

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import List, Optional

from agoralib import quota
from agoralib.parties import find_agy

CACHE = quota.AGORA_HOME / "models.json"
CACHE_TTL = 12 * 3600
CODEX_HOME = Path(os.getenv("CODEX_HOME") or Path.home() / ".codex")
EFFORTS = ["low", "medium", "high", "xhigh", "max"]
CLAUDE_ALIASES = [("fable", "Fable"), ("opus", "Opus"), ("sonnet", "Sonnet"), ("haiku", "Haiku")]


def _run(cmd: list, timeout: int = 60) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout, stdin=subprocess.DEVNULL).stdout


def claude_catalog() -> dict:
    settings = quota._read_json(Path.home() / ".claude" / "settings.json") or {}
    return {"models": [{"id": i, "label": f"{label}（最新版）"} for i, label in CLAUDE_ALIASES],
            "efforts": EFFORTS,
            "default": {"model": settings.get("model"), "effort": settings.get("effortLevel")}}


def antigravity_catalog() -> dict:
    models, error = [], None
    try:
        for line in _run([find_agy(), "models"]).splitlines():
            if "\t" in line:
                slug, label = line.split("\t", 1)
                models.append({"id": slug.strip(), "label": label.strip()})
    except (OSError, subprocess.TimeoutExpired) as e:
        error = str(e)
    if not models and not error:
        error = "agy models 沒有回傳任何模型"
    return {"models": models, "efforts": [], "default": {"model": None, "effort": None},
            **({"error": error} if error else {})}


def _codex_models_raw() -> Optional[List[dict]]:
    data = quota._read_json(CODEX_HOME / "models_cache.json")
    if not data:
        try:
            data = json.loads(_run([quota.find_codex(), "debug", "models"]))
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
            return None
    return data.get("models") if isinstance(data, dict) else data


def codex_catalog() -> dict:
    raw = _codex_models_raw()
    models = []
    for m in raw or []:
        if m.get("visibility", "list") != "list":
            continue
        efforts = [lv.get("effort") for lv in m.get("supported_reasoning_levels") or [] if lv.get("effort")]
        models.append({"id": m["slug"], "label": m.get("display_name") or m["slug"], "efforts": efforts,
                       "default_effort": m.get("default_reasoning_level")})
    default = {"model": None, "effort": None}
    try:
        toml = (CODEX_HOME / "config.toml").read_text(encoding="utf-8")
        top = re.split(r"^\[", toml, maxsplit=1, flags=re.M)[0]   # 只看最上層（第一個 [section] 之前）的設定
        for key, field in (("model", "model"), ("model_reasoning_effort", "effort")):
            m = re.search(rf'^{key}\s*=\s*"([^"]+)"', top, re.M)
            if m:
                default[field] = m.group(1)
    except OSError:
        pass
    efforts = sorted({e for m in models for e in m["efforts"]},
                     key=lambda e: (EFFORTS + [e]).index(e)) or EFFORTS
    return {"models": models, "efforts": efforts, "default": default,
            **({} if models else {"error": "找不到 Codex 模型目錄"})}


CATALOGS = {"claude": claude_catalog, "antigravity": antigravity_catalog, "codex": codex_catalog}


def catalog(refresh: bool = False) -> dict:
    cached = quota._read_json(CACHE)
    if cached and not refresh and time.time() - cached.get("fetched_at", 0) < CACHE_TTL:
        return cached
    data = {p: f() for p, f in CATALOGS.items()}
    data["fetched_at"] = time.time()
    try:
        quota._write_json(CACHE, data)
    except OSError:
        pass
    return data
