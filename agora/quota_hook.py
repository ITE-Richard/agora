"""
額度提醒 hook：讀快取（必要時在背景更新），把雙方剩餘額度與交接指示注入給 agent。

  python tools/agora/quota_hook.py claude-prompt   # Claude Code UserPromptSubmit：每次都附一行額度
  python tools/agora/quota_hook.py claude-tool     # Claude Code PostToolUse：只在需要交接/停工時提醒
  python tools/agora/quota_hook.py agy-pre         # Antigravity PreInvocation：首次呼叫附額度，需要時提醒

只讀檔案、不做網路請求，確保 hook 本身很快；快取過期時另開背景程序更新。
"""

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import quota  # noqa: E402

STATE_FILE = quota.AGORA_HOME / "hook_state.json"
NAMES = {"claude": "Claude", "antigravity": "Antigravity"}


def load_state() -> dict:
    return quota._read_json(STATE_FILE) or {}


def save_state(state: dict):
    quota._write_json(STATE_FILE, state)


def advice(me: str, usages: dict) -> tuple[str | None, str | None]:
    """回傳 (提醒等級, 指示文字)；等級用來避免同一個重置週期內重複提醒"""
    other = "antigravity" if me == "claude" else "claude"
    mine, theirs = usages[me], usages[other]
    if not quota.low(mine):
        return None, None
    if os.getenv("AGORA_INVOKED"):
        # 自己就是接手方：不再往回交接，寫好進度就停
        return f"worker-{mine.get('resets_at')}", (
            f"⚠️ 你（{NAMES[me]}）的額度剩 {mine['remaining_pct']:.0f}%。完成目前這一小步後，"
            f"把交接說明寫進進度檔，回覆最後一行寫「【額度不足】」並結束。")
    if quota.low(theirs):
        my_reset, their_reset = mine.get("resets_at"), (theirs or {}).get("resets_at")
        hand_off = bool(their_reset and my_reset and their_reset < my_reset)
        return f"both-{my_reset}", (
            f"⚠️ {NAMES[me]} 與 {NAMES[other]} 額度都低於 {quota.THRESHOLD_PCT:.0f}%，停止手邊工作。"
            + (f"{NAMES[other]} 較早重置（{quota.fmt_time(their_reset)}），依 relay skill 仍交接給它；接手程序會等它額度重置後才開始。"
               if hand_off else "依 relay skill 把目前進度寫進 Agora 討論串，不要交接。")
            + f"安排自己在 {quota.fmt_time(my_reset)} 重置後喚醒，然後停止工作。")
    return f"self-{mine.get('resets_at')}", (
        f"⚠️ {NAMES[me]} 額度剩 {mine['remaining_pct']:.0f}%（{quota.fmt_time(mine.get('resets_at'))} 重置），"
        f"{NAMES[other]} 尚有 {theirs['remaining_pct']:.0f}%。" if theirs else
        f"⚠️ {NAMES[me]} 額度剩 {mine['remaining_pct']:.0f}%（{quota.fmt_time(mine.get('resets_at'))} 重置）。"
    ) + f"立即依 relay skill 把手邊工作交接給 {NAMES[other]}，安排自己在重置後喚醒，然後停止工作。"


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "claude-prompt"
    (quota.AGORA_HOME / f"last_hook_{mode}").touch()  # 診斷用：確認 hook 有在執行
    if os.getenv(quota.PROBE_ENV):
        print("{}")
        return
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8") or "{}")
    except Exception:
        payload = {}

    usages = {"claude": quota.claude_usage(), "antigravity": quota.antigravity_usage()}
    max_age = quota.wanted_max_age(*usages.values())
    if any(quota.is_stale(u, max_age) for u in usages.values()):
        quota.refresh_in_background()

    me = "antigravity" if mode.startswith("agy") else "claude"
    level, text = advice(me, usages)
    status = "【額度】" + "｜".join(quota.describe(NAMES[k], u) for k, u in usages.items())

    state = load_state()
    key = f"{me}-alerted"
    already = level is not None and state.get(key) == level
    if level and not already:
        state[key] = level
        save_state(state)

    if mode == "claude-prompt":
        lines = [status] + ([text] if text else [])
        out = {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "\n".join(lines)}}
    elif mode == "claude-tool":
        out = {}
        if text and not already:
            out = {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": f"{status}\n{text}"}}
    else:  # agy-pre
        first = payload.get("invocationNum", 1) <= 1
        msgs = []
        if first:
            msgs.append(status)
        if text and (first or not already):
            msgs.append(text)
        out = {"injectSteps": [{"ephemeralMessage": "\n".join(msgs)}]} if msgs else {}

    sys.stdout.buffer.write(json.dumps(out, ensure_ascii=False).encode("utf-8"))


if __name__ == "__main__":
    main()
