"""
額度提醒 hook：讀快取（必要時在背景更新），把三方剩餘額度與交接指示注入給 agent。

  python <agora>/agoralib/quota_hook.py claude-prompt   # Claude Code UserPromptSubmit：每次都附一行額度
  python <agora>/agoralib/quota_hook.py claude-tool     # Claude Code PostToolUse：只在需要交接/停工時提醒
  python <agora>/agoralib/quota_hook.py agy-pre         # Antigravity PreInvocation：首次呼叫附額度，需要時提醒

只讀檔案、不做網路請求，確保 hook 本身很快；快取過期時另開背景程序更新。
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agoralib import config, quota  # noqa: E402

STATE_FILE = quota.AGORA_HOME / "hook_state.json"
NAMES = quota.LABELS


def load_state() -> dict:
    return quota._read_json(STATE_FILE) or {}


def save_state(state: dict):
    quota._write_json(STATE_FILE, state)


def advice(me: str, usages: dict) -> tuple[str | None, str | None]:
    """回傳 (提醒等級, 指示文字)；等級用來避免同一個重置週期內重複提醒"""
    mine = usages.get(me)
    if not quota.low(mine):
        return None, None
    if os.getenv("AGORA_INVOKED"):
        # 自己就是接手方：不再往回交接，寫好進度就停
        return f"worker-{mine.get('resets_at')}", (
            f"⚠️ 你（{NAMES[me]}）的額度剩 {mine['remaining_pct']:.0f}%。完成目前這一小步後，"
            f"把交接說明寫進進度檔，回覆最後一行寫「【額度不足】」並結束。")
    others = {p: u for p, u in usages.items() if p != me and u}
    healthy = {p: u for p, u in others.items() if not quota.low(u)}
    my_reset = mine.get("resets_at")
    if not healthy:
        earliest = min(others.items(), key=lambda kv: kv[1].get("resets_at") or float("inf"), default=(None, {}))
        party, u = earliest
        hand_off = bool(party and u.get("resets_at") and my_reset and u["resets_at"] < my_reset)
        return f"all-{my_reset}", (
            f"⚠️ 所有 AI 的額度都低於 {quota.THRESHOLD_PCT:.0f}%，停止手邊工作。"
            + (f"{NAMES[party]} 最早重置（{quota.fmt_time(u['resets_at'])}），依 relay skill 仍交接給它；"
               f"接手程序會等它額度重置後才開始。" if hand_off else "依 relay skill 把目前進度寫進 Agora 討論串，不要交接。")
            + f"安排自己在 {quota.fmt_time(my_reset)} 重置後喚醒，然後停止工作。")
    best = max(healthy, key=lambda p: healthy[p]["remaining_pct"])
    return f"self-{my_reset}", (
        f"⚠️ {NAMES[me]} 額度剩 {mine['remaining_pct']:.0f}%（{quota.fmt_time(my_reset)} 重置），"
        f"{NAMES[best]} 尚有 {healthy[best]['remaining_pct']:.0f}%。"
        f"立即依 relay skill 把手邊工作交接給 {NAMES[best]}，安排自己在重置後喚醒，然後停止工作。")


def project_parties() -> list:
    """目前專案啟用的 AI：只在這些 AI 之間提醒交接（找不到專案設定時視為全部啟用）"""
    start = os.getenv("CLAUDE_PROJECT_DIR") or os.getenv("AGORA_WORKSPACE") or os.getcwd()
    root = config.find_root(Path(start).resolve())
    return config.enabled(root) if root else list(quota.USAGE)


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

    me = "antigravity" if mode.startswith("agy") else "claude"
    parties = set(project_parties()) | {me}
    usages = {p: u for p, u in quota.all_usage().items() if p in parties}
    max_age = quota.wanted_max_age(*usages.values())
    if any(quota.is_stale(usages[p], max_age) for p in ("claude", "antigravity") if p in usages):
        quota.refresh_in_background()

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
