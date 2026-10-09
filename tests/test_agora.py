"""Agora 核心邏輯測試：不呼叫任何 AI CLI、不連網"""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agoralib import cli, config, jobs, prune, quota, quota_hook, shares, snapshot  # noqa: E402
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


class TestConsensus(unittest.TestCase):
    def test_agree_resets_when_someone_has_not_agreed(self):
        agreed = cli.agree(set(), "claude", "好 【已達成共識】")
        self.assertEqual(agreed, {"claude"})
        self.assertEqual(cli.agree(agreed, "codex", "還有問題"), set())
        self.assertEqual(cli.agree(agreed, "codex", "【已達成共識】"), {"claude", "codex"})

    def test_auto_stops_only_when_every_party_agrees(self):
        replies = ["【已達成共識】", "還有並發問題", "【已達成共識】", "【已達成共識】", "不該被呼叫"]
        calls = []

        def fake_invoke(ws, thread, state, party):
            calls.append(party)
            return replies[len(calls) - 1]

        with tempfile.TemporaryDirectory() as tmp:
            ws = cli.Workspace(Path(tmp))
            thread = ws.threads / "t1"
            thread.mkdir(parents=True)
            cli.save_state(thread, state_with(["claude", "codex"]))
            args = type("Args", (), {"thread": "t1", "rounds": 6, "order": None})()
            with patch.dict(os.environ, {jobs.JOB_ENV: str(thread)}), patch.object(cli, "invoke", fake_invoke), \
                    patch("sys.stdout", io.StringIO()):
                os.environ.pop(cli.NESTED_ENV, None)
                cli.cmd_auto(ws, args)
        self.assertEqual(calls, ["claude", "codex", "claude", "codex"])


SUMMARY = """## 共識
改用快照。
## 採納的意見
無
## 放棄的意見
無
## 保留的異議
無
## 工作單
### 範圍
修改 `a.txt` 與 src/new_module.py；不要碰 .env。版本 0.1.1。
### 步驟
1. 改 a.txt
### 驗收條件
python -m unittest
"""


class TestSummary(unittest.TestCase):
    def test_work_order_section(self):
        order = cli.work_order(SUMMARY)
        self.assertTrue(order.startswith("### 範圍"))
        self.assertIn("### 驗收條件", order)
        self.assertNotIn("## 共識", order)
        with self.assertRaises(ValueError):
            cli.work_order("## 共識\n沒有工作單")
        with self.assertRaises(ValueError):
            cli.work_order("## 工作單\n無\n## 其他\nx")

    def test_referenced_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.txt").write_text("a", encoding="utf-8")
            files = cli.referenced_files(root, cli.work_order(SUMMARY))
            self.assertEqual(set(files), {"a.txt", "src/new_module.py"})
            self.assertIsNone(files["src/new_module.py"])

    def run_summarize(self, root, reply="", low=()):
        ws = cli.Workspace(root)
        thread = ws.threads / "t1"
        thread.mkdir(parents=True)
        cli.save_state(thread, state_with(["claude", "codex"], [("human", "要不要改用快照？", False),
                                                               ("codex", "同意", True)]))
        prompts = []

        def fake_call(prompt, session_id, opts, work, workspace, timeout, scratch):
            prompts.append((prompt, session_id, work))
            return reply, "sess-1", "1s"

        usage = lambda p: {"remaining_pct": 1 if p in low else 80}  # noqa: E731
        with patch.dict(os.environ, {jobs.JOB_ENV: str(thread)}), patch.dict(cli.CALLERS, {"claude": fake_call}), \
                patch.object(quota, "usage", usage), patch("sys.stdout", io.StringIO()), \
                patch("sys.stderr", io.StringIO()):
            os.environ.pop(cli.NESTED_ENV, None)
            cli.cmd_summarize(ws, type("Args", (), {"thread": "t1", "by": "claude"})())
        return ws, thread, prompts

    def test_summarize_uses_new_session_and_full_transcript(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.txt").write_text("a", encoding="utf-8")
            ws, thread, prompts = self.run_summarize(root, SUMMARY)
            prompt, session_id, work = prompts[0]
            self.assertIsNone(session_id)
            self.assertFalse(work)
            self.assertIn("要不要改用快照？", prompt)
            self.assertIn("同意", prompt)                 # 包含 AI 自己透過 CLI 的回覆
            state = cli.load_state(thread)
            self.assertEqual(state["summary"]["messages"], 3)
            self.assertIsNone(state["summary"]["error"])
            self.assertIn("a.txt", state["summary"]["files"])
            self.assertTrue((thread / "summary.md").exists())
            self.assertEqual(cli.stale_reasons(root, state), [])
            handoff = cli.summary_handoff(ws, thread, state, confirmed=False)
            self.assertIn("### 驗收條件", handoff)

    def test_stale_summary_requires_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.txt").write_text("a", encoding="utf-8")
            ws, thread, _ = self.run_summarize(root, SUMMARY)
            state = cli.load_state(thread)
            state["messages"].append({"speaker": "human", "text": "再想想", "time": "now", "via_cli": False})
            (root / "a.txt").write_text("changed", encoding="utf-8")
            reasons = cli.stale_reasons(root, state)
            self.assertEqual(len(reasons), 2)
            with self.assertRaises(SystemExit) as ctx:
                cli.summary_handoff(ws, thread, state, confirmed=False)
            self.assertIn("過期", str(ctx.exception.code))
            self.assertIn("### 範圍", cli.summary_handoff(ws, thread, state, confirmed=True))

    def test_summary_without_work_order_refuses_assign(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws, thread, _ = self.run_summarize(Path(tmp), "## 共識\n還沒有結論")
            state = cli.load_state(thread)
            self.assertIn("找不到", state["summary"]["error"])
            with self.assertRaises(SystemExit) as ctx:
                cli.summary_handoff(ws, thread, state, confirmed=True)
            self.assertIn("無法依總結分派", str(ctx.exception.code))

    def test_low_quota_summarizer_suggests_other(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit) as ctx:
                self.run_summarize(Path(tmp), SUMMARY, low=("claude",))
            self.assertIn("--by", str(ctx.exception.code))


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, encoding="utf-8",
                          check=True).stdout


def make_repo(tmp) -> Path:
    root = Path(tmp)
    git(root, "init", "-q")
    git(root, "config", "user.name", "t")
    git(root, "config", "user.email", "t@t")
    (root / ".gitignore").write_text("ignored/\n", encoding="utf-8")
    (root / "a.txt").write_text("a\n", encoding="utf-8")
    (root / ".env").write_text("SECRET=1\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "init")
    return root


class TestSnapshot(unittest.TestCase):
    def test_snapshot_keeps_user_index_and_worktree(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(tmp)
            (root / "a.txt").write_text("a2\n", encoding="utf-8")
            (root / "staged.txt").write_text("s\n", encoding="utf-8")
            git(root, "add", "staged.txt")
            index = root / ".git" / "index"
            before_index, before_status = index.read_bytes(), git(root, "status", "--porcelain")
            snapshot.create(root, "t1")
            self.assertEqual(index.read_bytes(), before_index)
            self.assertEqual(git(root, "status", "--porcelain"), before_status)

    def test_snapshot_contents_and_secret_exclusion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(tmp)
            (root / "old_untracked.txt").write_text("u\n", encoding="utf-8")
            (root / ".env").write_text("SECRET=changed\n", encoding="utf-8")   # 已追蹤的機密檔
            (root / "ignored").mkdir()
            (root / "ignored" / "x.txt").write_text("x\n", encoding="utf-8")
            (root / ".agora").mkdir()
            (root / ".agora" / "state.json").write_text("{}", encoding="utf-8")
            base = snapshot.create(root, "t1")
            files = git(root, "ls-tree", "-r", "--name-only", base["commit"]).split()
            self.assertIn("old_untracked.txt", files)
            self.assertNotIn(".env", files)
            self.assertNotIn("ignored/x.txt", files)
            self.assertFalse(any(f.startswith(".agora") for f in files))
            self.assertEqual(git(root, "rev-parse", "refs/agora/t1").strip(), base["commit"])

    def test_changes_only_lists_changes_during_assignment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(tmp)
            (root / "a.txt").write_text("改到一半\n", encoding="utf-8")       # 分派前就有的修改
            (root / "draft.txt").write_text("草稿\n", encoding="utf-8")     # 分派前就有的未追蹤檔
            base = snapshot.create(root, "t1")
            self.assertEqual(snapshot.changes(root, base["commit"]), [])
            (root / "draft.txt").write_text("接手方改過\n", encoding="utf-8")
            (root / "new.txt").write_text("n\n", encoding="utf-8")
            (root / "a.txt").unlink()
            (root / ".env").write_text("SECRET=2\n", encoding="utf-8")
            got = {c["path"]: c["status"] for c in snapshot.changes(root, base["commit"])}
            self.assertEqual(got, {"draft.txt": "M", "new.txt": "A", "a.txt": "D"})

    def test_delete_ref_and_missing_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(tmp)
            base = snapshot.create(root, "t1")
            snapshot.delete(root, "t1")
            self.assertEqual(subprocess.run(["git", "rev-parse", "--verify", "-q", "refs/agora/t1"], cwd=root,
                                            capture_output=True).returncode, 1)
            snapshot.delete(root, "t1")   # 不存在時略過
            with self.assertRaises(snapshot.SnapshotError):
                snapshot.changes(root, "0" * 40)
            self.assertTrue(base["commit"])

    def test_not_a_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(snapshot.SnapshotError):
                snapshot.create(Path(tmp), "t1")

    def test_changes_command_and_prune_removes_ref(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(tmp)
            ws = cli.Workspace(root)
            thread = ws.threads / "t1"
            thread.mkdir(parents=True)
            state = state_with(["claude", "codex"])
            state["relay"] = {"worker": "codex", "status": "done", "started": "now",
                              "baseline": snapshot.create(root, "t1")}
            cli.save_state(thread, state)
            (root / "new.txt").write_text("n\n", encoding="utf-8")
            out = io.StringIO()
            with patch("sys.stdout", out):
                cli.cmd_changes(ws, type("Args", (), {"thread": "t1", "json": True})())
            self.assertEqual(json.loads(out.getvalue())["files"], [{"status": "A", "path": "new.txt"}])
            prune.delete_thread(thread, {"sessions": []}, with_sessions=False)
            self.assertEqual(subprocess.run(["git", "rev-parse", "--verify", "-q", "refs/agora/t1"], cwd=root,
                                            capture_output=True).returncode, 1)

    def test_changes_without_baseline_explains(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = cli.Workspace(Path(tmp))
            thread = ws.threads / "t1"
            thread.mkdir(parents=True)
            state = state_with(["codex"])
            state["relay"] = {"worker": "codex", "status": "done", "baseline": {"error": "工作區不是 git repo"}}
            cli.save_state(thread, state)
            with self.assertRaises(SystemExit) as ctx:
                cli.cmd_changes(ws, type("Args", (), {"thread": "t1", "json": False})())
            self.assertIn("不是 git repo", str(ctx.exception.code))


class TestAntigravityMode(unittest.TestCase):
    def run_call(self, work):
        from agoralib import parties
        seen = {}

        def fake_run(cmd, workspace, stdin_text, timeout):
            seen["cmd"] = cmd
            return [{"status": "SUCCESS", "response": "ok", "conversation_id": "c1"}], ""

        with tempfile.TemporaryDirectory() as tmp, patch.object(parties, "_run", fake_run):
            parties.call_antigravity("hi", None, {}, work, Path(tmp), 60, Path(tmp))
        return seen["cmd"]

    def test_discussion_cannot_edit(self):
        self.assertNotIn("--mode", self.run_call(work=False))

    def test_work_accepts_edits(self):
        cmd = self.run_call(work=True)
        self.assertEqual(cmd[cmd.index("--mode") + 1], "accept-edits")


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
            self.assertFalse((Path(tmp) / ".gitignore").exists())          # 不是 git repo：不碰 .gitignore
            self.assertTrue((Path(tmp) / ".claude" / "skills" / "agora" / "SKILL.md").exists())

    def test_install_excludes_agora_files_from_git(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as home:
            root = make_repo(tmp)
            gitignore_before = (root / ".gitignore").read_bytes()
            (root / ".agents").mkdir()
            (root / ".agents" / "hooks.json").write_text("{}", encoding="utf-8")   # 使用者原本就有的檔案
            exclude = root / ".git" / "info" / "exclude"
            exclude.write_bytes(b"# mine\r\n*.log\r\n")
            with patch.object(Path, "home", return_value=Path(home)):
                for _ in range(2):
                    cli.cmd_install(cli.Workspace(root), None)
            text = exclude.read_bytes().decode("utf-8")
            self.assertTrue(text.startswith("# mine\r\n*.log\r\n"))          # 保留原內容與行尾
            self.assertEqual(text.count(cli.EXCLUDE_BEGIN), 1)
            for p in ("/.agora/", "/.claude/skills/agora/", "/.claude/settings.local.json", "/.agents/skills/relay/",
                      "/AGENTS.md"):
                self.assertIn(p + "\r\n", text)
            self.assertNotIn("/.agents/hooks.json", text)                  # 不是 install 建立的
            self.assertEqual((root / ".gitignore").read_bytes(), gitignore_before)
            self.assertEqual(git(root, "status", "--porcelain", "--untracked-files=all").strip(), "?? .agents/hooks.json")

    def test_install_warns_about_tracked_agora_files(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as home:
            root = make_repo(tmp)
            skill = root / ".claude" / "skills" / "agora" / "SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text("old", encoding="utf-8")
            git(root, "add", "-A")
            git(root, "commit", "-qm", "skills")
            out = io.StringIO()
            with patch.object(Path, "home", return_value=Path(home)), patch("sys.stdout", out):
                cli.cmd_install(cli.Workspace(root), None)
            self.assertIn("已被 git 追蹤", out.getvalue())
            self.assertIn(".claude/skills/agora/SKILL.md", out.getvalue())

    def test_install_uses_agora_command_when_installed(self):
        from agoralib import parties
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as home:
            with patch.object(Path, "home", return_value=Path(home)), patch.object(parties, "_CMD", "agora"):
                cli.cmd_install(cli.Workspace(Path(tmp)), None)
            skill = (Path(tmp) / ".claude" / "skills" / "agora" / "SKILL.md").read_text(encoding="utf-8")
            self.assertIn("T=$(agora new", skill)
            self.assertNotIn("agora.py new", skill)
            allow = json.loads((Path(home) / ".gemini" / "antigravity-cli" / "settings.json")
                               .read_text(encoding="utf-8"))["permissions"]["allow"]
            self.assertIn("command(agora quota)", allow)
            self.assertIn(f"command({parties.LEGACY_CMD} quota)", allow)   # 舊指令保留相容

    def test_exclude_in_subdirectory_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(tmp)
            sub = root / "pkg"
            sub.mkdir()
            exclude, _ = cli.git_exclude(sub, [".agora/"])
            self.assertIn("/pkg/.agora/", exclude.read_text(encoding="utf-8"))

    def test_install_removes_old_antigravity_write_rules(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as home:
            ws = cli.Workspace(Path(tmp))
            settings = Path(home) / ".gemini" / "antigravity-cli" / "settings.json"
            settings.parent.mkdir(parents=True)
            keep = "write_file(D:/somewhere/else)"
            settings.write_text(json.dumps({"permissions": {"allow": [
                f"write_file({ws.root})", f"write_file({ws.root.as_posix()})", keep]}}), encoding="utf-8")
            with patch.object(Path, "home", return_value=Path(home)):
                cli.cmd_install(ws, None)
            allow = json.loads(settings.read_text(encoding="utf-8"))["permissions"]["allow"]
            self.assertFalse(any(r.startswith("write_file(") and r != keep for r in allow))
            self.assertIn(keep, allow)

    def test_install_skips_disabled_parties(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as home:
            config.update_parties(Path(tmp), disable=["antigravity", "codex"])
            with patch.object(Path, "home", return_value=Path(home)):
                cli.cmd_install(cli.Workspace(Path(tmp)), None)
            self.assertTrue((Path(tmp) / ".claude" / "skills" / "agora" / "SKILL.md").exists())
            self.assertFalse((Path(tmp) / ".agents").exists())
            self.assertFalse((Path(tmp) / "AGENTS.md").exists())
            self.assertFalse((Path(home) / ".gemini").exists())

    def test_install_substitutes_custom_agora_root(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as custom_agora:
            ws = cli.Workspace(Path(tmp))
            custom_root = Path(custom_agora)
            shutil.copytree(cli.AGORA_ROOT_DIR / "skills", custom_root / "skills")
            (custom_root / "agoralib").mkdir()
            shutil.copyfile(cli.AGORA_ROOT_DIR / "agoralib" / "quota_hook.py", custom_root / "agoralib" / "quota_hook.py")
            with patch.object(Path, "home", return_value=Path(home)), patch.object(cli, "AGORA_ROOT_DIR", custom_root):
                cli.cmd_install(ws, None)
            skill = (Path(tmp) / ".claude" / "skills" / "agora" / "SKILL.md").read_text(encoding="utf-8")
            self.assertIn(custom_root.as_posix(), skill)
            agents = (Path(tmp) / "AGENTS.md").read_text(encoding="utf-8")
            self.assertIn(custom_root.as_posix(), agents)


class TestPartySettings(unittest.TestCase):
    def test_defaults_to_all_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(config.enabled(Path(tmp)), ["claude", "antigravity", "codex"])

    def test_update_and_clear_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config.update_parties(root, disable=["antigravity"], models={"codex": "gpt-x"}, efforts={"claude": "high"})
            self.assertEqual(config.enabled(root), ["claude", "codex"])
            s = config.party_settings(root)
            self.assertEqual((s["codex"]["model"], s["claude"]["effort"]), ("gpt-x", "high"))
            config.update_parties(root, models={"codex": "default"})
            self.assertIsNone(config.party_settings(root)["codex"]["model"])

    def test_cannot_disable_everyone(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                config.update_parties(Path(tmp), disable=["claude", "antigravity", "codex"])
            self.assertFalse(config.config_path(Path(tmp)).exists())

    def test_thread_model_overrides_project_setting(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = cli.Workspace(Path(tmp))
            config.update_parties(ws.root, models={"codex": "proj-model", "claude": "opus"}, efforts={"codex": "low"})
            s = state_with(["claude", "codex"])
            s["parties"]["codex"]["model"] = "thread-model"
            self.assertEqual(cli.call_opts(ws, s, "codex"), {"model": "thread-model", "effort": "low"})
            self.assertEqual(cli.call_opts(ws, s, "claude"), {"model": "opus", "effort": None})

    def test_disabled_party_is_not_asked_to_reply(self):
        s = state_with(["claude", "antigravity", "codex"])
        self.assertEqual(cli.targets_for(s, "claude", None, ["claude", "codex"]), ["codex"])
        with self.assertRaises(SystemExit):
            cli.targets_for(s, HUMAN, "antigravity", ["claude", "codex"])

    def test_hook_only_hands_off_within_project(self):
        u = lambda pct: {"remaining_pct": pct, "resets_at": time.time() + 3600, "updated_at": time.time()}  # noqa: E731
        with tempfile.TemporaryDirectory() as tmp:
            config.update_parties(Path(tmp), disable=["codex"])
            with patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": tmp}):
                parties = quota_hook.project_parties()
        self.assertEqual(parties, ["claude", "antigravity"])
        usages = {p: x for p, x in {"claude": u(3), "antigravity": u(40), "codex": u(90)}.items() if p in parties}
        self.assertIn("交接給 Antigravity", quota_hook.advice("claude", usages)[1])


class TestShares(unittest.TestCase):
    def ok(self, pct=50):
        return {"remaining_pct": pct, "resets_at": time.time() + 3600, "updated_at": time.time()}

    def test_assignments_follow_shares(self):
        weights = {"claude": 5, "codex": 3, "antigravity": 2}
        usages = {p: self.ok() for p in weights}
        work = {}
        for _ in range(10):
            p = shares.pick(weights, weights, work, usages)
            work[p] = work.get(p, 0) + 1
        self.assertEqual(work, {"claude": 5, "codex": 3, "antigravity": 2})

    def test_skips_zero_share_and_low_quota(self):
        usages = {"claude": self.ok(), "codex": self.ok(2), "antigravity": self.ok()}
        self.assertEqual(shares.pick(usages, {"claude": 0, "codex": 9, "antigravity": 1}, {}, usages), "antigravity")
        self.assertIsNone(shares.pick(["claude", "codex"], {"claude": 0, "codex": 1}, {}, usages))

    def test_recent_work_counts_window_and_active_relays(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".agora" / "threads" / "t1").mkdir(parents=True)
            old = {"time": time.time() - 30 * 86400, "party": "codex", "thread": "t0"}
            shares.log_path(root).write_text(json.dumps(old) + "\n", encoding="utf-8")
            shares.record(root, "codex", "t1")
            shares.record(root, "claude", "t1")
            (root / ".agora" / "threads" / "t1" / "state.json").write_text(
                json.dumps({"relay": {"worker": "codex", "status": "running"}}), encoding="utf-8")
            self.assertEqual(shares.recent_work(root), {"codex": 2, "claude": 1})
            self.assertEqual(shares.recent_work(root, include_active=False), {"codex": 1, "claude": 1})

    def test_recent_work_ignores_malformed_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bad_lines = [
                json.dumps({"time": time.time()}),                 # missing party
                json.dumps({"time": time.time(), "party": None}),   # None party
                "not json",
                json.dumps({"time": time.time(), "party": "codex"}),
            ]
            shares.log_path(root).parent.mkdir(parents=True)
            shares.log_path(root).write_text("\n".join(bad_lines) + "\n", encoding="utf-8")
            self.assertEqual(shares.recent_work(root), {"codex": 1})

    def test_hook_hands_off_by_share_not_just_quota(self):
        usages = {"claude": self.ok(3), "antigravity": self.ok(90), "codex": self.ok(40)}
        _, text = quota_hook.advice("claude", usages, {"antigravity": 1, "codex": 3}, {"antigravity": 0, "codex": 1})
        self.assertIn("交接給 Codex", text)

    def test_hook_with_no_eligible_partner_stops(self):
        level, text = quota_hook.advice("claude", {"claude": self.ok(3), "codex": self.ok(90)}, {"codex": 0})
        self.assertTrue(level.startswith("none-"))
        self.assertIn("沒有其他可接手的 AI", text)

    def test_share_setting_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config.update_parties(root, shares={"claude": "5", "codex": "0"})
            self.assertEqual(config.shares(root), {"claude": 5, "antigravity": 1, "codex": 0})
            with self.assertRaises(ValueError):
                config.update_parties(root, shares={"codex": "-1"})
            config.update_parties(root, shares={"claude": "default"})
            self.assertEqual(config.shares(root)["claude"], 1)


class TestPrune(unittest.TestCase):
    def make(self, root, name, idle_days=40, **state):
        thread = root / name
        thread.mkdir(parents=True)
        (thread / "state.json").write_text(json.dumps({"topic": name, "parties": {}, "messages": [], **state}),
                                           encoding="utf-8")
        old = time.time() - idle_days * 86400
        for f in thread.iterdir():
            os.utime(f, (old, old))
        return thread

    def test_only_unneeded_threads_are_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            threads = [
                self.make(root, "old"),
                self.make(root, "old-done", relay={"status": "done", "worker": "codex"}),
                self.make(root, "recent", idle_days=3),
                self.make(root, "kept", keep=True),
                self.make(root, "paused", relay={"status": "stopped", "worker": "codex"}),
                self.make(root, "failed", relay={"status": "failed", "worker": "codex"}),
            ]
            locked = self.make(root, "locked")
            (locked / ".lock").touch()
            candidates, kept = prune.scan(threads + [locked], 30)
            self.assertEqual(sorted(c["id"] for c in candidates), ["old", "old-done"])
            self.assertEqual(len(kept), 5)
            # 指定討論串時不看天數，但保留、未完成、使用中照樣不刪
            candidates, _ = prune.scan(threads, None)
            self.assertEqual(sorted(c["id"] for c in candidates), ["old", "old-done", "recent"])

    def test_external_sessions_are_never_deleted(self):
        state = {"parties": {"codex": {"session_id": "agora-1"}},
                 "sessions": [{"party": "codex", "id": "agora-1", "external": False},
                              {"party": "claude", "id": "mine", "external": True}],
                 "relay": {"worker": "claude", "session_id": "mine", "external_session": "mine"}}
        self.assertEqual(prune.sessions_of(state), [("codex", "agora-1")])
        legacy = {"parties": {}, "relay": {"worker": "claude", "session_id": "unknown-origin"}}
        self.assertEqual(prune.sessions_of(legacy), [])     # 舊版分派無法確認來源，不刪

    def test_delete_with_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            thread = self.make(Path(tmp), "old")
            info = prune.scan([thread], 30)[0][0]
            info["sessions"] = [{"party": "codex", "id": "x1"}, {"party": "claude", "id": "x2"}]
            calls = []
            fake = {"codex": lambda i: calls.append(("codex", i)) or "ok",
                    "claude": lambda i: (_ for _ in ()).throw(RuntimeError("boom"))}
            with patch.dict(prune.DELETERS, fake):
                out = prune.delete_thread(thread, info, True)
            self.assertEqual(calls, [("codex", "x1")])
            self.assertEqual(len(out["errors"]), 1)
            self.assertFalse(thread.exists())

    def test_delete_antigravity_cleans_wal_and_shm(self):
        with tempfile.TemporaryDirectory() as tmp:
            agy_home = Path(tmp)
            conv = agy_home / "conversations"
            conv.mkdir(parents=True)
            db = conv / "sess-1.db"
            wal = conv / "sess-1.db-wal"
            shm = conv / "sess-1.db-shm"
            for f in (db, wal, shm):
                f.write_text("dummy", encoding="utf-8")
            with patch.object(prune, "AGY_HOME", agy_home):
                prune.delete_antigravity("sess-1")
            self.assertFalse(db.exists())
            self.assertFalse(wal.exists())
            self.assertFalse(shm.exists())

    def test_delete_thread_ignores_unknown_party(self):
        with tempfile.TemporaryDirectory() as tmp:
            thread = self.make(Path(tmp), "old")
            info = prune.scan([thread], 30)[0][0]
            info["sessions"] = [{"party": "unknown_party", "id": "u1"}, {"party": None, "id": "n1"}]
            out = prune.delete_thread(thread, info, True)
            self.assertEqual(out["done"], [])
            self.assertEqual(out["errors"], [])
            self.assertFalse(thread.exists())

    def test_remember_session(self):
        s = state_with(["codex"])
        cli.remember_session(s, "codex", "a")
        cli.remember_session(s, "codex", "a")
        cli.remember_session(s, "claude", "b", external=True)
        self.assertEqual(s["sessions"], [{"party": "codex", "id": "a", "external": False},
                                         {"party": "claude", "id": "b", "external": True}])


class TestJobs(unittest.TestCase):
    def test_run_records_exit_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            thread = Path(tmp)
            with self.assertRaises(SystemExit):
                jobs.run(thread, lambda: None)
            self.assertEqual((jobs.read(thread)["status"], jobs.read(thread)["exit"]), ("done", 0))

            def fail():
                sys.exit("此討論串正在等待回覆")
            with patch("sys.stderr"), self.assertRaises(SystemExit):
                jobs.run(thread, fail)
            self.assertEqual((jobs.read(thread)["status"], jobs.read(thread)["exit"]), ("failed", 1))

    def test_dead_running_job_becomes_interrupted(self):
        with tempfile.TemporaryDirectory() as tmp:
            thread = Path(tmp)
            jobs.update(thread, status="running", pid=99999999, started_ts=time.time() - 3600)
            self.assertIsNone(jobs.active(thread))
            self.assertEqual(jobs.read(thread)["status"], "interrupted")
            jobs.update(thread, status="running", pid=os.getpid())
            self.assertIsNotNone(jobs.active(thread))

    def test_follow_relays_output_and_exit_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            thread = Path(tmp)
            (thread / "job.log").write_text("═══ Codex ═══\n收到\n", encoding="utf-8")
            (thread / "job.err").write_text("…等待 Codex 回覆\n", encoding="utf-8")
            jobs.update(thread, status="failed", exit=3)
            out, err = io.StringIO(), io.StringIO()
            with patch("sys.stdout", out), patch("sys.stderr", err):
                code = jobs.follow(thread, poll=0)
            self.assertEqual(code, 3)
            self.assertIn("收到", out.getvalue())
            self.assertIn("等待 Codex", err.getvalue())

    def test_background_process_survives_caller_tree_kill(self):
        if not sys.platform.startswith("win"):
            self.skipTest("Windows 專用")
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out.txt"
            pid = jobs.spawn([sys.executable, "-c", "import time; time.sleep(30)"], Path(tmp), out, out)
            try:
                self.assertTrue(jobs.pid_alive(pid))
            finally:
                jobs.kill_tree(pid)


class TestCliResolvers(unittest.TestCase):
    def test_find_codex_env_override(self):
        with patch.dict(os.environ, {"CODEX_PATH": "/custom/codex"}):
            self.assertEqual(quota.find_codex(), "/custom/codex")

    def test_find_claude_env_override(self):
        with patch.dict(os.environ, {"CLAUDE_PATH": "/custom/claude"}):
            self.assertEqual(quota.find_claude(), "/custom/claude")

    def test_agora_cmd_falls_back_to_script_path(self):
        from agoralib import parties
        with patch.object(parties, "_CMD", None), patch.object(parties.shutil, "which", return_value=None):
            self.assertEqual(parties.agora_cmd(), parties.LEGACY_CMD)
        with patch.object(parties, "_CMD", None), \
                patch.object(parties.shutil, "which", return_value=str(Path(tempfile.gettempdir()) / "agora.exe")):
            self.assertEqual(parties.agora_cmd(), parties.LEGACY_CMD)   # 不在目前 Python 的 Scripts 底下

    def test_find_agy_env_override(self):
        from agoralib import parties
        with patch.dict(os.environ, {"AGY_PATH": "/custom/agy"}):
            self.assertEqual(parties.find_agy(), "/custom/agy")


class TestLock(unittest.TestCase):
    def test_thread_lock_handles_vanished_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            thread = Path(tmp)
            lock = thread / ".lock"
            lock.touch()
            # 模擬另一進程在 FileExists 後正好刪除鎖
            orig_stat = Path.stat
            stat_calls = 0

            def flaky_stat(*args, **kwargs):
                nonlocal stat_calls
                stat_calls += 1
                if stat_calls == 1:
                    lock.unlink(missing_ok=True)
                    raise FileNotFoundError("Lock file deleted concurrently")
                return orig_stat(lock, *args, **kwargs)

            with patch.object(Path, "stat", side_effect=flaky_stat):
                with cli.thread_lock(thread, wait=5):
                    self.assertTrue(lock.exists())
            self.assertFalse(lock.exists())
            self.assertGreaterEqual(stat_calls, 1)


class TestVersion(unittest.TestCase):
    def test_version_defined_and_cli_flag(self):
        from agoralib import __version__
        self.assertEqual(__version__, "0.1.1")
        out = io.StringIO()
        with patch("sys.stdout", out), self.assertRaises(SystemExit) as cm:
            cli.main(["--version"])
        self.assertEqual(cm.exception.code, 0)
        self.assertIn("0.1.1", out.getvalue())


class TestQuotaWindows(unittest.TestCase):
    def test_codex_window_label(self):
        self.assertEqual(quota.codex_window_label("primary", 300), "5 小時")
        self.assertEqual(quota.codex_window_label("primary", 10080), "7 天")
        self.assertEqual(quota.codex_window_label("primary", 60), "1 小時")
        self.assertEqual(quota.codex_window_label("primary", 2880), "2 天")
        self.assertEqual(quota.codex_window_label("primary", 45), "45 分鐘")
        self.assertEqual(quota.codex_window_label("primary", None), "主要")
        self.assertEqual(quota.codex_window_label("secondary", None), "次要")

    def test_claude_usage_multi_windows(self):
        now = time.time()
        snap = {
            "updated_at": int(now),
            "five_hour": {"used_percentage": 10.0, "resets_at": now + 7200},
            "seven_day": {"used_percentage": 30.0, "resets_at": now + 86400 * 5},
        }
        with patch.object(quota, "_read_json", return_value=snap):
            u = quota.claude_usage()
            self.assertIsNotNone(u)
            self.assertEqual(u["window"], "seven_day")
            self.assertEqual(u["window_label"], "7 天")
            self.assertEqual(u["remaining_pct"], 70.0)
            self.assertIn("five_hour", u["windows"])
            self.assertIn("seven_day", u["windows"])
            self.assertEqual(u["windows"]["five_hour"]["remaining_pct"], 90.0)
            self.assertEqual(u["windows"]["five_hour"]["label"], "5 小時")
            self.assertEqual(u["windows"]["seven_day"]["remaining_pct"], 70.0)
            self.assertEqual(u["windows"]["seven_day"]["label"], "7 天")

    def test_codex_usage_windows(self):
        now = time.time()
        snap = {
            "updated_at": int(now),
            "plan": "pro",
            "primary": {"used_percentage": 40.0, "resets_at": now + 3600, "window_minutes": 300},
            "secondary": {"used_percentage": 15.0, "resets_at": now + 86400 * 7, "window_minutes": 10080},
        }
        with patch.object(quota, "_read_json", return_value=snap):
            u = quota.codex_usage()
            self.assertIsNotNone(u)
            self.assertEqual(u["window"], "primary")
            self.assertEqual(u["window_label"], "5 小時")
            self.assertEqual(u["remaining_pct"], 60.0)
            self.assertEqual(u["windows"]["primary"]["label"], "5 小時")
            self.assertEqual(u["windows"]["secondary"]["label"], "7 天")
            self.assertEqual(u["windows"]["secondary"]["remaining_pct"], 85.0)

    def test_describe_formatting(self):
        now = time.time()
        u1 = {"remaining_pct": 71.0, "resets_at": now + 3600, "updated_at": now, "window": "seven_day", "window_label": "7 天"}
        self.assertIn("7 天，", quota.describe("Claude", u1))

        u2 = {"remaining_pct": 100.0, "resets_at": now + 3600, "updated_at": now}
        self.assertNotIn("，重置", quota.describe("Antigravity", u2))
        self.assertIn("100%", quota.describe("Antigravity", u2))


if __name__ == "__main__":
    unittest.main()
