"""Agora 核心邏輯測試：不呼叫任何 AI CLI、不連網"""

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agoralib import cli, quota, quota_hook  # noqa: E402
from agoralib.parties import HUMAN, canonical  # noqa: E402


def state_with(parties, messages=()):
    return {"topic": "t", "parties": {p: {"session_id": None, "seen": 0, "model": None} for p in parties},
            "messages": [{"speaker": s, "text": t, "time": "now", "via_cli": v} for s, t, v in messages]}


class TestTargets(unittest.TestCase):
    def test_ai_sender_defaults_to_all_others(self):
        s = state_with(["claude", "antigravity", "codex"])
        self.assertEqual(cli.targets_for(s, "claude", None), ["antigravity", "codex"])

    def test_human_sender_defaults_to_record_only(self):
        s = state_with(["claude", "codex"])
        self.assertEqual(cli.targets_for(s, HUMAN, None), [])
        self.assertEqual(cli.targets_for(s, HUMAN, "codex"), ["codex"])
        self.assertEqual(cli.targets_for(s, HUMAN, "all"), ["claude", "codex"])

    def test_aliases(self):
        self.assertEqual(canonical("Richard"), HUMAN)
        self.assertEqual(canonical("agy"), "antigravity")


class TestPrompt(unittest.TestCase):
    def test_first_prompt_has_preamble_and_skips_own_cli_replies(self):
        ws = cli.Workspace(Path(tempfile.gettempdir()))
        s = state_with(["claude", "codex"], [("claude", "開場", False), ("codex", "codex 的回覆", True)])
        prompt = cli.build_prompt(ws, s, "codex")
        self.assertIn("【Agora 討論環境說明】", prompt)
        self.assertIn("開場", prompt)
        self.assertNotIn("codex 的回覆", prompt)        # 不重送它自己透過 CLI 產生的回覆

    def test_followup_prompt_only_has_unseen(self):
        ws = cli.Workspace(Path(tempfile.gettempdir()))
        s = state_with(["claude", "codex"], [("claude", "舊訊息", False), ("claude", "新訊息", False)])
        s["parties"]["codex"].update(session_id="abc", seen=1)
        prompt = cli.build_prompt(ws, s, "codex")
        self.assertNotIn("討論環境說明", prompt)
        self.assertNotIn("舊訊息", prompt)
        self.assertIn("新訊息", prompt)


class TestCodexQuota(unittest.TestCase):
    def test_reads_latest_rate_limits_from_session_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            day = Path(tmp) / "2026" / "10" / "08"
            day.mkdir(parents=True)
            f = day / "rollout-2026-10-08T07-00-00-abc123.jsonl"
            older = {"payload": {"rate_limits": {"primary": {"used_percent": 10.0, "resets_at": 2_000_000_000}}}}
            newer = {"payload": {"info": {"rate_limits": {"primary": {"used_percent": 40.0, "window_minutes": 10080,
                                                                      "resets_at": 2_000_000_000}}}}}
            f.write_text(json.dumps(older) + "\n" + json.dumps(newer) + "\n", encoding="utf-8")
            snap = Path(tmp) / "codex_quota.json"
            with patch.object(quota, "CODEX_SESSIONS", Path(tmp)), patch.object(quota, "CODEX_SNAPSHOT", snap):
                self.assertTrue(quota.update_codex_snapshot_from_session("abc123"))
                u = quota.codex_usage()
        self.assertEqual(u["remaining_pct"], 60.0)


class TestHookAdvice(unittest.TestCase):
    def usage(self, pct, reset_in=3600):
        return {"remaining_pct": pct, "resets_at": time.time() + reset_in, "updated_at": time.time()}

    def test_low_self_hands_to_party_with_most_quota(self):
        level, text = quota_hook.advice("claude", {"claude": self.usage(3), "antigravity": self.usage(40),
                                                   "codex": self.usage(80)})
        self.assertTrue(level.startswith("self-"))
        self.assertIn("交接給 Codex", text)

    def test_all_low_hands_to_earliest_reset_only_if_earlier(self):
        level, text = quota_hook.advice("claude", {"claude": self.usage(3, 7200), "antigravity": self.usage(2, 600),
                                                   "codex": self.usage(1, 9000)})
        self.assertTrue(level.startswith("all-"))
        self.assertIn("Antigravity 最早重置", text)

    def test_healthy_self_no_advice(self):
        self.assertEqual(quota_hook.advice("claude", {"claude": self.usage(50), "codex": self.usage(1)}), (None, None))


class TestInstall(unittest.TestCase):
    def test_install_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as home:
            ws = cli.Workspace(Path(tmp))
            (Path(tmp) / "AGENTS.md").write_text("# 既有說明\n", encoding="utf-8")
            with patch.object(Path, "home", return_value=Path(home)):
                for _ in range(2):
                    cli.cmd_install(ws, None)
            settings = json.loads((Path(tmp) / ".claude" / "settings.local.json").read_text(encoding="utf-8"))
            self.assertEqual(len(settings["hooks"]["UserPromptSubmit"]), 1)
            self.assertEqual(len(settings["hooks"]["PostToolUse"]), 1)
            agents = (Path(tmp) / "AGENTS.md").read_text(encoding="utf-8")
            self.assertEqual(agents.count("<!-- agora:begin -->"), 1)
            self.assertIn("# 既有說明", agents)
            allow = json.loads((Path(home) / ".gemini" / "antigravity-cli" / "settings.json")
                               .read_text(encoding="utf-8"))["permissions"]["allow"]
            self.assertEqual(len(allow), len(set(allow)))
            self.assertEqual((Path(tmp) / ".gitignore").read_text(encoding="utf-8").count(".agora/"), 1)
            self.assertTrue((Path(tmp) / ".claude" / "skills" / "agora" / "SKILL.md").exists())


if __name__ == "__main__":
    unittest.main()
