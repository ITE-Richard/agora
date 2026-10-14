"""Agora 核心邏輯測試：不呼叫任何 AI CLI、不連網"""

import argparse
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

from agoralib import cli, config, jobs, parties, prune, quota, quota_hook, shares, snapshot  # noqa: E402
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
        self.assertEqual(cli.targets_for(s, HUMAN, "all", ["claude", "codex"]), ["claude", "codex"])

    def test_newly_enabled_party_joins_unless_fixed(self):
        s = state_with(["claude", "codex"])
        enabled = ["claude", "antigravity", "codex"]
        self.assertEqual(cli.thread_parties(s, enabled), ["claude", "codex", "antigravity"])
        self.assertEqual(cli.targets_for(s, "claude", None, enabled), ["codex", "antigravity"])
        self.assertEqual(cli.thread_parties(s, ["claude"]), ["claude"])           # 取消勾選的不再參與
        s["parties_fixed"] = True                                                   # new --parties 指定的
        self.assertEqual(cli.thread_parties(s, enabled), ["claude", "codex"])

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

    def test_marker_must_start_a_line(self):
        self.assertTrue(cli.has_marker("分析如下\n\n【已達成共識】改用快照", cli.CONSENSUS))
        self.assertTrue(cli.has_marker("說明\n**【工作完成】**", "【工作完成】"))
        self.assertTrue(cli.has_marker("> 【已達成共識】", cli.CONSENSUS))
        self.assertTrue(cli.has_marker("同意，【已達成共識】：改用快照", cli.CONSENSUS))
        self.assertFalse(cli.has_marker("鎖的問題還沒解決，我還不會寫【已達成共識】。", cli.CONSENSUS))
        self.assertFalse(cli.has_marker("我同意，但請先回覆「【已達成共識】」的條件", cli.CONSENSUS))
        self.assertFalse(cli.has_marker("做完後會在最後一行寫【工作完成】", "【工作完成】"))

    def test_auto_stops_only_when_every_party_agrees(self):
        replies = ["【已達成共識】", "還有並發問題", "【已達成共識】", "【已達成共識】", "不該被呼叫"]
        calls = []

        def fake_invoke(ws, thread, party):
            calls.append(party)
            return replies[len(calls) - 1]

        with tempfile.TemporaryDirectory() as tmp:
            ws = cli.Workspace(Path(tmp))
            config.update_parties(ws.root, disable=["antigravity"])
            thread = ws.threads / "t1"
            thread.mkdir(parents=True)
            cli.save_state(thread, state_with(["claude", "codex"]))
            args = type("Args", (), {"thread": "t1", "rounds": 6, "order": None})()
            with patch.dict(os.environ, {jobs.JOB_ENV: str(thread)}), patch.object(cli, "invoke", fake_invoke), \
                    patch("sys.stdout", io.StringIO()):
                os.environ.pop(cli.NESTED_ENV, None)
                cli.cmd_auto(ws, args)
        self.assertEqual(calls, ["claude", "codex", "claude", "codex"])

    def run_auto(self, fake_invoke, rounds=6, before_round=None):
        with tempfile.TemporaryDirectory() as tmp:
            ws = cli.Workspace(Path(tmp))
            config.update_parties(ws.root, disable=["antigravity"])
            thread = ws.threads / "t1"
            thread.mkdir(parents=True)
            cli.save_state(thread, state_with(["claude", "codex"]))
            args = type("Args", (), {"thread": "t1", "rounds": rounds, "order": None})()
            with patch.dict(os.environ, {jobs.JOB_ENV: str(thread)}), \
                    patch.object(cli, "invoke", lambda w, t, p: fake_invoke(w, p)), \
                    patch("sys.stdout", io.StringIO()), patch("sys.stderr", io.StringIO()):
                os.environ.pop(cli.NESTED_ENV, None)
                try:
                    cli.cmd_auto(ws, args)
                    return None
                except SystemExit as e:
                    return e.code

    def test_auto_picks_up_party_enabled_mid_run(self):
        calls = []

        def fake_invoke(ws, party):
            calls.append(party)
            if len(calls) == 2:   # 第二輪進行中，使用者在面板勾選 Antigravity
                config.update_parties(ws.root, enable=["antigravity"])
            return "繼續討論"

        self.run_auto(fake_invoke, rounds=4)
        self.assertEqual(calls, ["claude", "codex", "antigravity", "claude"])

    def test_auto_skips_failed_party(self):
        calls = []

        def fake_invoke(ws, party):
            calls.append(party)
            if party == "claude":
                raise cli.CallFailed("呼叫 Claude Code 失敗")
            return "繼續討論"

        code = self.run_auto(fake_invoke, rounds=4)
        self.assertEqual(calls, ["claude", "codex", "claude", "codex"])
        self.assertIn("沒有回覆", code)
        calls.clear()
        code = self.run_auto(lambda ws, p: (calls.append(p), (_ for _ in ()).throw(cli.CallFailed("x")))[1])
        self.assertEqual(calls, ["claude", "codex"])                 # 連續一整圈都失敗就結束
        self.assertIn("所有 AI 都沒有回覆", code)


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

    def test_referenced_files_includes_new_root_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            files = cli.referenced_files(Path(tmp), "建立 new_module.py，版本改成 0.2.0，見 e.g. 說明")
            self.assertIn("new_module.py", files)
            self.assertNotIn("0.2.0", files)
            state = {"messages": [], "summary": {"messages": 0, "files": files}}
            self.assertEqual(cli.stale_reasons(Path(tmp), state), [])
            (Path(tmp) / "new_module.py").write_text("x", encoding="utf-8")   # 總結後有人先建立了
            self.assertEqual(len(cli.stale_reasons(Path(tmp), state)), 1)

    def run_summarize(self, root, reply="", low=()):
        ws = cli.Workspace(root)
        thread = ws.threads / "t1"
        thread.mkdir(parents=True)
        cli.save_state(thread, state_with(["claude", "codex"], [("human", "要不要改用快照？", False),
                                                               ("codex", "同意", True)]))
        prompts = []

        def fake_call(prompt, session_id, opts, work, workspace, timeout, scratch, live=None):
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

    def test_changes_in_subdirectory_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(tmp)
            sub = root / "pkg"
            sub.mkdir()
            (sub / "a.py").write_text("1\n", encoding="utf-8")
            git(root, "add", "-A")
            git(root, "commit", "-qm", "pkg")
            base = snapshot.create(sub, "t1")
            (sub / "a.py").write_text("2\n", encoding="utf-8")
            (root / "outside.txt").write_text("o\n", encoding="utf-8")   # 工作區外的變更不列出
            self.assertEqual(snapshot.changes(sub, base["commit"]), [{"status": "M", "path": "a.py"}])
            # 擴充套件以 sha:./路徑 從工作區讀基準內容
            self.assertEqual(git(sub, "show", f"{base['commit']}:./a.py"), "1\n")

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


class TestLive(unittest.TestCase):
    def make_live(self, tmp):
        return parties.Live(Path(tmp) / "live" / "discussion.md", "Claude Code", Path(tmp))

    def test_claude_events(self):
        with tempfile.TemporaryDirectory() as tmp, patch("sys.stderr", io.StringIO()) as err:
            live = self.make_live(tmp)
            delta = lambda t: {"type": "stream_event", "event": {"type": "content_block_delta",  # noqa: E731
                                                                  "delta": {"type": "text_delta", "text": t}}}
            for e in (delta("我先讀"), {"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "name": "Read", "input": {"file_path": str(Path(tmp) / "src" / "a.py")}}]}},
                      {"type": "stream_event", "event": {"type": "message_start"}}, delta("結論")):
                parties.claude_event(live, e)
            live.write(force=True)
            body = live.path.read_text(encoding="utf-8")
            self.assertIn("- Read src/a.py", body)
            self.assertIn("我先讀\n\n結論", body)
            self.assertIn("Claude Code：Read src/a.py", err.getvalue())
            live.close()
            self.assertFalse(live.path.exists())

    def test_codex_and_antigravity_events(self):
        with tempfile.TemporaryDirectory() as tmp, patch("sys.stderr", io.StringIO()):
            live = self.make_live(tmp)
            parties.codex_event(live, {"type": "item.started", "item": {
                "type": "command_execution",
                "command": '"C:\\\\WINDOWS\\\\powershell.exe" -Command "Get-Content -LiteralPath \'a.py\'"'}})
            parties.codex_event(live, {"type": "item.completed", "item": {"type": "agent_message", "text": "看完了"}})
            parties.antigravity_event(live, {"event": "step_update", "step_update": {
                "step_type": "tool", "state": "ACTIVE", "tool_name": "view_file",
                "tool_info": {"parameters": {"AbsolutePath": str(Path(tmp) / "b.py")}}}})
            parties.antigravity_event(live, {"event": "step_update", "step_update": {
                "step_type": "agent_response", "state": "ACTIVE", "text_delta": "版本 0.2"}})
            self.assertEqual(live.actions, ["執行 Get-Content -LiteralPath 'a.py'", "view_file b.py"])
            self.assertEqual(live.text, "看完了\n\n版本 0.2")

    def test_run_streams_events_and_times_out(self):
        script = "import json,sys,time\nfor i in range(3): print(json.dumps({'n': i}), flush=True)\n" \
                 "time.sleep(float(sys.argv[1]))\n"
        seen = []
        with tempfile.TemporaryDirectory() as tmp:
            events, _ = parties._run([sys.executable, "-c", script, "0"], Path(tmp), "", 30, seen.append)
            self.assertEqual([e["n"] for e in events], [0, 1, 2])
            self.assertEqual(seen, events)
            start = time.time()
            with self.assertRaises(subprocess.TimeoutExpired):   # 逾時 = timeout + 60 秒，這裡是 1 秒
                parties._run([sys.executable, "-c", script, "30"], Path(tmp), "", -59, None)
            self.assertLess(time.time() - start, 15)

    def test_stop_discards_partial_reply(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = cli.Workspace(Path(tmp))
            thread = ws.threads / "t1"
            thread.mkdir(parents=True)
            cli.save_state(thread, state_with(["claude"]))
            (thread / "transcript.md").write_text("", encoding="utf-8")
            live = cli.live_path(thread, "discussion")
            live.parent.mkdir()
            live.write_text("半則回覆", encoding="utf-8")
            jobs.update(thread, status="running", pid=999999, current="claude")
            with patch.object(jobs, "active", return_value={"pid": None, "current": "claude"}), \
                    patch("sys.stdout", io.StringIO()):
                os.environ.pop(cli.NESTED_ENV, None)
                cli.cmd_stop(ws, type("Args", (), {"thread": "t1"})())
            self.assertFalse(live.exists())
            self.assertNotIn("半則回覆", (thread / "transcript.md").read_text(encoding="utf-8"))


class TestDiscussionFailures(unittest.TestCase):
    def setup_thread(self, tmp):
        ws = cli.Workspace(Path(tmp))
        thread = ws.threads / "t1"
        thread.mkdir(parents=True)
        cli.save_state(thread, state_with(["claude", "antigravity", "codex"]))
        (thread / "transcript.md").write_text("", encoding="utf-8")
        return ws, thread

    def send(self, ws, callers):
        args = type("Args", (), {"thread": "t1", "sender": "human", "to": "all", "message": "請查證", "file": None})()
        with patch.dict(os.environ, {jobs.JOB_ENV: str(ws.threads / "t1")}), patch.dict(cli.CALLERS, callers), \
                patch.object(cli, "git_status", return_value=""), patch("sys.stdout", io.StringIO()), \
                patch("sys.stderr", io.StringIO()):
            os.environ.pop(cli.NESTED_ENV, None)
            cli.cmd_send(ws, args)

    def test_denied_round_is_retried_in_same_session(self):
        calls = []

        def agy(prompt, session_id, opts, work, workspace, timeout, scratch, live=None):
            calls.append((prompt, session_id))
            if len(calls) == 1:
                raise parties.CallError("denied", denied=True, session_id="conv-1")
            return "改用讀檔查證後的回覆", "conv-1", "1s"

        ok = lambda *a, **k: ("好", "s", "1s")  # noqa: E731
        with tempfile.TemporaryDirectory() as tmp:
            ws, thread = self.setup_thread(tmp)
            self.send(ws, {"claude": ok, "antigravity": agy, "codex": ok})
            self.assertEqual(calls[1], (cli.DENIED_RETRY, "conv-1"))
            state = cli.load_state(thread)
            self.assertIn("改用讀檔查證後的回覆", [m["text"] for m in state["messages"]])
            self.assertEqual(state["parties"]["antigravity"]["session_id"], "conv-1")
            self.assertIn("重試一次", (thread / "transcript.md").read_text(encoding="utf-8"))

    def test_send_continues_after_a_party_fails(self):
        def broken(*a, **k):
            raise parties.CallError("timeout-ish failure")

        ok = lambda *a, **k: ("Codex 的回覆", "s", "1s")  # noqa: E731
        with tempfile.TemporaryDirectory() as tmp:
            ws, thread = self.setup_thread(tmp)
            with self.assertRaises(SystemExit) as ctx:
                self.send(ws, {"claude": broken, "antigravity": broken, "codex": ok})
            self.assertIn("沒有回覆", str(ctx.exception.code))
            texts = [m["text"] for m in cli.load_state(thread)["messages"]]
            self.assertIn("Codex 的回覆", texts)

    def test_others_can_write_while_waiting_for_reply(self):
        """等 AI 回覆期間不持有鎖：接手程序能更新 relay，期間加的訊息不會被蓋掉，下次仍轉給它"""
        def claude(prompt, session_id, opts, work, workspace, timeout, scratch, live=None):
            cli.update_relay(thread, status="done")                 # 同一串的接手程序在這時寫入
            with cli.thread_lock(thread, wait=1):
                state = cli.load_state(thread)
                cli.add_message(thread, state, "codex", "接手方的回報", via_cli=True)
                cli.save_state(thread, state)
            return "Claude 的回覆", "s1", "1s"

        with tempfile.TemporaryDirectory() as tmp:
            ws, thread = self.setup_thread(tmp)
            args = type("Args", (), {"thread": "t1", "sender": "human", "to": "claude", "message": "請查證", "file": None})()
            with patch.dict(os.environ, {jobs.JOB_ENV: str(thread)}), patch.dict(cli.CALLERS, {"claude": claude}), \
                    patch.object(cli, "git_status", return_value=""), patch("sys.stdout", io.StringIO()), \
                    patch("sys.stderr", io.StringIO()):
                os.environ.pop(cli.NESTED_ENV, None)
                cli.cmd_send(ws, args)
            state = cli.load_state(thread)
            self.assertEqual(state["relay"]["status"], "done")
            self.assertEqual([m["text"] for m in state["messages"]], ["請查證", "接手方的回報", "Claude 的回覆"])
            self.assertIn("接手方的回報", cli.build_prompt(ws, state, "claude"))
            self.assertNotIn("Claude 的回覆", cli.build_prompt(ws, state, "claude"))


class TestSubagents(unittest.TestCase):
    def capture(self, caller, opts, events=None):
        seen = {}

        def fake_run(cmd, workspace, stdin_text, timeout, on_event=None, extra_env=None):
            seen.update(cmd=cmd, stdin=stdin_text, env=extra_env or {})
            return events or [], ""

        with tempfile.TemporaryDirectory() as tmp, patch.object(parties, "_run", fake_run):
            try:
                result = caller("問題", None, opts, False, Path(tmp), 60, Path(tmp))
            except parties.CallError:
                result = None
        return seen, result

    def test_setting_defaults_and_update(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertTrue(config.party_settings(root)["codex"]["subagents"])
            settings = config.update_parties(root, subagents={"codex": "off"})
            self.assertFalse(settings["codex"]["subagents"])
            self.assertTrue(config.update_parties(root, subagents={"codex": "on"})["codex"]["subagents"])
            with self.assertRaises(ValueError):
                config.update_parties(root, subagents={"codex": "maybe"})

    def test_claude_disallows_task_only_when_off(self):
        ok = [{"type": "result", "result": "好", "session_id": "s"}]
        seen, _ = self.capture(parties.call_claude, {"subagents": False}, ok)
        self.assertIn("Task", seen["cmd"])
        self.assertLess(seen["cmd"].index("--disallowedTools"), seen["cmd"].index("Task"))
        seen, _ = self.capture(parties.call_claude, {"subagents": True}, ok)
        self.assertNotIn("Task", seen["cmd"])

    def test_codex_disables_feature_asks_in_prompt_and_flags_usage(self):
        events = [{"type": "thread.started", "thread_id": "t"},
                  {"type": "item.started", "item": {"type": "collab_tool_call", "tool": "wait"}},
                  {"type": "item.completed", "item": {"type": "agent_message", "text": "完成"}}]
        with patch.object(parties.quota, "update_codex_snapshot_from_session"):
            seen, result = self.capture(parties.call_codex, {"subagents": False}, events)
            self.assertIn("multi_agent", seen["cmd"])
            self.assertTrue(seen["stdin"].startswith(parties.NO_SUBAGENT_NOTE))
            self.assertIn("仍使用了內建子代理", result[2])
            seen, result = self.capture(parties.call_codex, {"subagents": True}, events)
            self.assertNotIn("multi_agent", seen["cmd"])
            self.assertNotIn("子代理", result[2])

    def test_antigravity_passes_setting_to_hook(self):
        ok = [{"event": "result", "result": {"status": "SUCCESS", "response": "好", "conversation_id": "c"}}]
        seen, _ = self.capture(parties.call_antigravity, {"subagents": False}, ok)
        self.assertEqual(seen["env"], {parties.SUBAGENTS_ENV: "0"})
        seen, _ = self.capture(parties.call_antigravity, {}, ok)
        self.assertEqual(seen["env"], {parties.SUBAGENTS_ENV: "1"})

    def test_agy_tool_hook_denies_only_when_off(self):
        hook = Path(cli.AGORA_ROOT_DIR) / "agoralib" / "quota_hook.py"
        for value, decision in (("0", "deny"), ("1", "allow"), (None, "allow")):
            env = {k: v for k, v in os.environ.items() if k != "AGORA_SUBAGENTS"}
            if value is not None:
                env["AGORA_SUBAGENTS"] = value
            out = subprocess.run([sys.executable, str(hook), "agy-tool"], input='{"toolCall":{"name":"invoke_subagent"}}',
                                 capture_output=True, text=True, encoding="utf-8", env=env, timeout=60).stdout
            self.assertEqual(json.loads(out)["decision"], decision, value)

    def test_install_adds_subagent_hook_without_quotes(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as home:
            ws = cli.Workspace(Path(tmp))
            with patch.object(Path, "home", return_value=Path(home)):
                cli.cmd_install(ws, None)
            hooks = json.loads((ws.root / ".agents" / "hooks.json").read_text(encoding="utf-8"))
            groups = hooks["agora-subagents"]["PreToolUse"]
            self.assertEqual({g["matcher"] for g in groups}, set(parties.AGY_SUBAGENT_TOOLS))
            self.assertTrue(all('"' not in h["command"] for g in groups for h in g["hooks"]))
            with patch.object(Path, "home", return_value=Path(home)):
                self.assertEqual(cli.install_status(ws)["state"], "installed")

    @unittest.skipUnless(sys.platform.startswith("win"), "8.3 短路徑只在 Windows")
    def test_no_space_path_uses_short_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "Has Space"
            folder.mkdir()
            (folder / "x.py").write_text("", encoding="utf-8")
            text = parties.no_space_path(folder / "x.py")
            self.assertTrue(" " not in text or text.startswith('"'))


class TestAntigravityMode(unittest.TestCase):
    def run_call(self, work):
        from agoralib import parties
        seen = {}

        def fake_run(cmd, workspace, stdin_text, timeout, on_event=None, extra_env=None):
            seen["cmd"] = cmd
            return [{"event": "result", "result": {"status": "SUCCESS", "response": "ok", "conversation_id": "c1"}}], ""

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

    def test_install_status(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as home,                 patch.object(Path, "home", return_value=Path(home)):
            ws = cli.Workspace(Path(tmp))
            config.update_parties(ws.root, models={"claude": "opus"})        # 只有設定檔（例如在面板改過模型）
            self.assertEqual(cli.install_status(ws)["state"], "missing")
            cli.cmd_install(ws, None)
            self.assertEqual(cli.install_status(ws), {**cli.install_status(ws), "state": "installed",
                                                      "missing": [], "outdated": []})
            # Antigravity 指令權限是全域設定：缺了新版的指令（例如 build）算需要更新
            settings = Path(home) / ".gemini" / "antigravity-cli" / "settings.json"
            data = json.loads(settings.read_text(encoding="utf-8"))
            full = list(data["permissions"]["allow"])
            data["permissions"]["allow"] = [r for r in full if not r.endswith(" build)")]
            settings.write_text(json.dumps(data), encoding="utf-8")
            status = cli.install_status(ws)
            self.assertEqual((status["state"], status["outdated"]), ("outdated", ["Antigravity 指令權限"]))
            data["permissions"]["allow"] = full
            settings.write_text(json.dumps(data), encoding="utf-8")
            skill = ws.root / ".claude" / "skills" / "agora" / "SKILL.md"
            skill.write_text("舊版內容", encoding="utf-8")
            status = cli.install_status(ws)
            self.assertEqual((status["state"], status["outdated"]), ("outdated", ["Claude Code skill「agora」"]))
            (ws.root / ".agents" / "hooks.json").unlink()
            self.assertEqual(cli.install_status(ws)["state"], "partial")
            out = io.StringIO()
            with patch("sys.stdout", out):
                cli.cmd_install(ws, type("Args", (), {"status": True, "json": True})())
            self.assertIn("Antigravity 額度 hook", json.loads(out.getvalue())["missing"])

    def test_install_quotes_agora_path_with_spaces(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as home, \
                tempfile.TemporaryDirectory() as parent:
            custom_root = Path(parent) / "Richard Clayd" / ".agora" / "core"
            shutil.copytree(cli.AGORA_ROOT_DIR / "skills", custom_root / "skills")
            (custom_root / "agoralib").mkdir()
            with patch.object(Path, "home", return_value=Path(home)), patch.object(cli, "AGORA_ROOT_DIR", custom_root), \
                    patch.object(parties, "_CMD", parties.LEGACY_CMD):
                cli.cmd_install(cli.Workspace(Path(tmp)), None)
            quoted = f'python "{custom_root.as_posix()}/agora.py"'
            skill = (Path(tmp) / ".claude" / "skills" / "agora" / "SKILL.md").read_text(encoding="utf-8")
            self.assertIn(f"T=$({quoted} new", skill)
            hooks = (Path(tmp) / ".claude" / "settings.local.json").read_text(encoding="utf-8")
            self.assertIn('quota_hook.py\\" claude-prompt', hooks)          # JSON 內的引號
            allow = json.loads((Path(home) / ".gemini" / "antigravity-cli" / "settings.json")
                               .read_text(encoding="utf-8"))["permissions"]["allow"]
            self.assertIn(f"command({quoted} quota)", allow)

    def test_quote_path(self):
        self.assertEqual(parties.quote_path("D:/github/agora/agora.py"), "D:/github/agora/agora.py")
        self.assertEqual(parties.quote_path("C:/Users/A B/x.py"), '"C:/Users/A B/x.py"')


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
            self.assertEqual(cli.call_opts(ws, s, "codex"), {"model": "thread-model", "effort": "low", "sandbox": None, "subagents": True, "add_dirs": []})
            self.assertEqual(cli.call_opts(ws, s, "claude"), {"model": "opus", "effort": None, "sandbox": None, "subagents": True, "add_dirs": []})

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
                started = jobs.pid_started(pid)
                self.assertTrue(jobs.alive({"pid": pid, "pid_started": started}))
                # PID 被別的程序重複使用：建立時間對不上就視為已結束
                self.assertFalse(jobs.alive({"pid": pid, "pid_started": started - 100}))
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

    def test_agora_cmd_requires_editable_install_of_this_copy(self):
        import sysconfig
        from importlib import metadata

        class Dist:
            def __init__(self, direct):
                self.metadata = {"Name": "agora-cli"}
                self.direct = direct

            def read_text(self, name):
                return json.dumps(self.direct) if name == "direct_url.json" else None

        url = parties.AGORA_ROOT.as_uri()
        exe = Path(sysconfig.get_path("scripts")) / "agora.exe"
        for direct, expected in (({"url": url, "dir_info": {"editable": True}}, True),
                                 ({"url": url, "dir_info": {}}, False),                    # 一般安裝（複製）
                                 ({"url": "file:///elsewhere", "dir_info": {"editable": True}}, False)):
            with patch.object(metadata, "distributions", return_value=[Dist(direct)]):
                self.assertEqual(parties._installed_here(exe), expected, direct)

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
        self.assertEqual(__version__, "0.2.7")
        out = io.StringIO()
        with patch("sys.stdout", out), self.assertRaises(SystemExit) as cm:
            cli.main(["--version"])
        self.assertEqual(cm.exception.code, 0)
        self.assertIn("0.2.7", out.getvalue())


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


class TestBuildAndSandbox(unittest.TestCase):
    def run_cmd(self, func, ws):
        out = io.StringIO()
        with patch("sys.stdout", out), self.assertRaises(SystemExit) as cm:
            func(ws, argparse.Namespace())
        return cm.exception.code, out.getvalue()

    def test_run_configured_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            proc = cli.run_configured_command([sys.executable, "-c", "print('hello_world')"], cwd)
            self.assertEqual((proc.returncode, proc.stdout.strip()), (0, "hello_world"))
            # 清單不經 shell：參數裡的 & 原樣傳給程式
            proc = cli.run_configured_command([sys.executable, "-c", "import sys; print(sys.argv[1])", "a&b"], cwd)
            self.assertEqual(proc.stdout.strip(), "a&b")
            # 清單的清單依序執行，遇到失敗就停
            proc = cli.run_configured_command([[sys.executable, "-c", "print('one')"],
                                               [sys.executable, "-c", "import sys; sys.exit(3)"],
                                               [sys.executable, "-c", "print('never')"]], cwd)
            self.assertEqual(proc.returncode, 3)
            self.assertIn("one", proc.stdout)
            self.assertNotIn("never", proc.stdout)
            self.assertEqual(cli.run_configured_command(["no-such-command-agora"], cwd).returncode, 127)
            with self.assertRaises(ValueError):
                cli.run_configured_command([["a"], "b"], cwd)

    def test_build_and_check_are_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = cli.Workspace(Path(tmp))
            self.assertIn("未設定 build", str(self.run_cmd(cli.cmd_build, ws)[0]))
            config.save(ws.root, {"build": [sys.executable, "-c", "print('BUILD_OK')"],
                                  "check": [sys.executable, "-c", "print('CHECK_OK')"]})
            code, out = self.run_cmd(cli.cmd_build, ws)
            self.assertEqual(code, 0)
            self.assertIn("BUILD_OK", out)
            code, out = self.run_cmd(cli.cmd_check, ws)
            self.assertEqual(code, 0)
            self.assertIn("CHECK_OK", out)
            self.assertNotIn("BUILD_OK", out)
            config.save(ws.root, {"build": [sys.executable, "-c", "import sys; sys.exit(2)"]})
            self.assertEqual(self.run_cmd(cli.cmd_build, ws)[0], 1)

    def test_check_syntax_without_writing_pycache(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = cli.Workspace(Path(tmp))
            subprocess.run(["git", "init", "-q"], cwd=ws.root, check=True)
            (ws.root / "good.py").write_text("x = 1\n", encoding="utf-8")
            (ws.root / "bad.py").write_text("def f(:\n", encoding="utf-8")
            code, out = self.run_cmd(cli.cmd_check, ws)
            self.assertEqual(code, 1)
            self.assertIn("2 個檔案，1 個錯誤", out)
            self.assertIn("bad.py", out)
            self.assertFalse((ws.root / "__pycache__").exists())

    def test_check_without_tests_dir_skips_unittest(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = cli.Workspace(Path(tmp))
            code, out = self.run_cmd(cli.cmd_check, ws)
            self.assertEqual(code, 0)
            self.assertIn("略過測試", out)

    def test_sandbox_setting(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = config.update_parties(root, sandboxes={"codex": "danger-full-access"})
            self.assertEqual(settings["codex"]["sandbox"], "danger-full-access")
            self.assertIsNone(settings["claude"]["sandbox"])
            self.assertIsNone(config.update_parties(root, sandboxes={"codex": "default"})["codex"]["sandbox"])
            with self.assertRaises(ValueError):
                config.update_parties(root, sandboxes={"codex": "invalid-sandbox"})
            with self.assertRaises(ValueError):
                config.update_parties(root, sandboxes={"claude": "danger-full-access"})
            config.update_parties(root, models={"codex": "m"}, sandboxes={"codex": "danger-full-access"})
            opts = cli.call_opts(cli.Workspace(root), state_with(["codex"]), "codex")
            self.assertEqual(opts["sandbox"], "danger-full-access")
            self.assertEqual("、".join(opts[f] for f in config.FIELDS if opts.get(f)), "m")   # 等待訊息不顯示沙箱

    def codex_cmd(self, ws, opts, work):
        called = {}

        def fake_run(cmd, *args, **kwargs):
            called["cmd"] = cmd
            return [{"type": "thread.started", "thread_id": "th1"},
                    {"type": "item.completed", "item": {"type": "agent_message", "text": "OK"}}], ""

        with patch("agoralib.parties._run", side_effect=fake_run), \
                patch("agoralib.quota.update_codex_snapshot_from_session"):
            parties.call_codex("prompt", None, opts, work, ws, 30, ws)
        cmd = called["cmd"]
        return cmd[cmd.index("-s") + 1], [cmd[i + 1] for i, x in enumerate(cmd) if x == "--add-dir"]

    def test_call_codex_sandbox_and_add_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            (ws / "custom_dir").mkdir()
            opts = {"add_dirs": ["custom_dir", "missing_dir"]}
            self.assertEqual(self.codex_cmd(ws, opts, True), ("workspace-write", [str(ws / "custom_dir")]))
            self.assertFalse((ws / "missing_dir").exists())          # 不存在的略過，不替使用者建立
            self.assertFalse((ws / ".agents").exists())
            # 討論與總結一律唯讀，不受沙箱設定影響
            for sandbox in ("danger-full-access", "workspace-write", None):
                self.assertEqual(self.codex_cmd(ws, {**opts, "sandbox": sandbox}, False), ("read-only", []))
            self.assertEqual(self.codex_cmd(ws, {**opts, "sandbox": "danger-full-access"}, True),
                             ("danger-full-access", []))

    def test_guard_config_restores_protected_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, thread = Path(tmp), Path(tmp) / "thread"
            thread.mkdir()
            config.save(root, {"check": ["python", "-V"], "parties": {"codex": {"share": 2}}})
            original = cli.read_config_text(root)
            self.assertFalse(cli.guard_config(root, original, thread, "codex"))
            # 接手方把自己改成完整存取、換掉 build；同時 Richard 在面板改了模型 → 只還原受保護的設定
            config.save(root, {"check": ["python", "-V"], "build": "evil",
                               "parties": {"codex": {"share": 2, "sandbox": "danger-full-access", "model": "m"}}})
            self.assertTrue(cli.guard_config(root, original, thread, "codex"))
            self.assertEqual(config.load(root), {"check": ["python", "-V"], "parties": {"codex": {"share": 2, "model": "m"}}})
            self.assertIn("已還原", (thread / "transcript.md").read_text(encoding="utf-8"))
            # 改成無法解析 → 整個檔案還原
            config.config_path(root).write_text("{", encoding="utf-8")
            self.assertTrue(cli.guard_config(root, original, thread, "codex"))
            self.assertEqual(cli.read_config_text(root), original)
            # 只改其他設定 → 不動
            config.update_parties(root, models={"claude": "opus"})
            self.assertFalse(cli.guard_config(root, original, thread, "codex"))

    def test_worker_cannot_run_build_it_just_rewrote(self):
        """接手方在同一輪內改了 build 再執行 agora build：仍執行這輪開始時的指令，結束後設定檔還原"""
        with tempfile.TemporaryDirectory() as tmp:
            ws = cli.Workspace(Path(tmp))
            config.save(ws.root, {"build": [sys.executable, "-c", "print('ORIGINAL_BUILD')"]})
            thread = ws.threads / "t1"
            thread.mkdir(parents=True)
            state = state_with(["claude", "codex"])
            state["relay"] = {"worker": "codex", "from": "claude", "handoff": "做事", "rounds": 0, "status": "starting"}
            cli.save_state(thread, state)
            (thread / "transcript.md").write_text("", encoding="utf-8")
            seen = {}

            def codex(prompt, session_id, opts, work, workspace, timeout, scratch, live=None):
                data = config.load(workspace)
                data["build"] = [sys.executable, "-c", "print('EVIL_BUILD')"]
                config.save(workspace, data)
                out = io.StringIO()
                with patch.dict(os.environ, opts["env"]), patch("sys.stdout", out), self.assertRaises(SystemExit):
                    cli.cmd_build(cli.Workspace(workspace), argparse.Namespace())
                seen["out"], seen["prompt"] = out.getvalue(), prompt
                return "完成\n【工作完成】", "s1", "1s"

            with patch.dict(cli.CALLERS, {"codex": codex}), patch.object(cli, "party_usage", return_value=None), \
                    patch.object(cli, "git_status", return_value=""), patch("sys.stdout", io.StringIO()):
                cli.cmd_work(ws, type("Args", (), {"thread": "t1"})())
            self.assertIn("ORIGINAL_BUILD", seen["out"])
            self.assertNotIn("EVIL_BUILD", seen["out"])
            self.assertIn("agora build", seen["prompt"].replace(cli.agora_cmd(), "agora"))
            self.assertEqual(config.load(ws.root)["build"], [sys.executable, "-c", "print('ORIGINAL_BUILD')"])
            self.assertIn("已還原", (thread / "transcript.md").read_text(encoding="utf-8"))

    def test_guard_files_restores_hooks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, thread = Path(tmp), Path(tmp) / "thread"
            thread.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            (root / ".claude").mkdir()
            (root / ".claude" / "settings.local.json").write_text('{"hooks": {}}', encoding="utf-8")
            before = cli.protected_files(root)
            self.assertEqual(cli.guard_files(root, before, thread, "codex"), [])
            # 接手方加了 hook、新建 Antigravity hook 與 git pre-commit
            (root / ".claude" / "settings.local.json").write_text('{"hooks": {"PostToolUse": "evil"}}', encoding="utf-8")
            (root / ".agents").mkdir()
            (root / ".agents" / "hooks.json").write_text("{}", encoding="utf-8")
            hook = root / ".git" / "hooks" / "pre-commit"
            hook.write_text("evil", encoding="utf-8")
            self.assertEqual(len(cli.guard_files(root, before, thread, "codex")), 3)
            self.assertEqual((root / ".claude" / "settings.local.json").read_text(encoding="utf-8"), '{"hooks": {}}')
            self.assertFalse((root / ".agents" / "hooks.json").exists())
            self.assertFalse(hook.exists())
            self.assertIn("已還原", (thread / "transcript.md").read_text(encoding="utf-8"))

    def test_claude_worker_cannot_edit_hook_settings(self):
        seen = {}

        def fake_run(cmd, *a, **k):
            seen["cmd"] = cmd
            return [{"type": "result", "result": "ok", "session_id": "s"}], ""

        with tempfile.TemporaryDirectory() as tmp, patch.object(parties, "_run", fake_run):
            parties.call_claude("做事", None, {}, True, Path(tmp), 60, Path(tmp))
        denied = seen["cmd"][seen["cmd"].index("--disallowedTools"):]
        for rule in ("Edit(./.claude/**)", "Edit(./.agents/**)", "Edit(./.git/**)"):
            self.assertIn(rule, denied)

    def test_python39_compatible(self):
        """pyproject 宣告支援 3.9：X | None、list[...] 等標註在 3.9 執行時會出錯，要有 from __future__ import annotations"""
        import ast
        for path in (Path(cli.__file__).parent).glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), feature_version=(3, 9))
            future = any(isinstance(n, ast.ImportFrom) and n.module == "__future__" for n in tree.body)
            annotations = [n.returns for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))] \
                + [n.annotation for n in ast.walk(tree) if isinstance(n, (ast.arg, ast.AnnAssign))]
            modern = [a for a in annotations if a is not None and any(
                isinstance(x, ast.BinOp) and isinstance(x.op, ast.BitOr)
                or isinstance(x, ast.Subscript) and isinstance(x.value, ast.Name) and x.value.id in ("list", "dict", "tuple", "set")
                for x in ast.walk(a))]
            self.assertTrue(future or not modern, f"{path.name} 用了 3.10+ 的型別標註，請加 from __future__ import annotations")

    def test_quota_error_ignores_ids_and_numbers(self):
        self.assertFalse(parties.quota_error({"session_id": "a4291c", "duration_ms": 14290, "result": "boom"}))
        self.assertFalse(parties.quota_error(None, "File x.py, line 1429"))
        self.assertTrue(parties.quota_error({"result": "Claude AI usage limit reached"}))
        self.assertTrue(parties.quota_error(None, "HTTP 429 Too Many Requests"))

    def run_worker(self, codex, usage=None):
        with tempfile.TemporaryDirectory() as tmp:
            ws = cli.Workspace(Path(tmp))
            thread = ws.threads / "t1"
            thread.mkdir(parents=True)
            state = state_with(["claude", "codex"])
            state["relay"] = {"worker": "codex", "from": "claude", "handoff": "做事", "rounds": 0, "status": "starting"}
            cli.save_state(thread, state)
            (thread / "transcript.md").write_text("", encoding="utf-8")
            sleeps = []
            with patch.dict(cli.CALLERS, {"codex": codex}), patch.object(cli, "party_usage", return_value=usage), \
                    patch.object(cli, "git_status", return_value=""), patch.object(quota, "refresh"), \
                    patch.object(cli, "sleep_until", lambda t, ts: sleeps.append(ts) or True), \
                    patch.object(cli, "MAX_WORK_ROUNDS", 2), patch("sys.stdout", io.StringIO()):
                cli.cmd_work(ws, type("Args", (), {"thread": "t1"})())
            return cli.load_state(thread)["relay"], sleeps

    def test_worker_backs_off_when_quota_error_but_usage_unknown(self):
        calls = []

        def codex(*a, **k):
            calls.append(1)
            if len(calls) <= 3:
                raise parties.CallError("usage limit", quota_exhausted=True)
            return "做完一步", "s1", "1s"

        relay, sleeps = self.run_worker(codex)
        self.assertEqual(len(sleeps), 3)                  # 每次都先等一段時間，不立刻重試
        self.assertEqual(relay["status"], "max_rounds")
        self.assertEqual(relay["rounds"], 2)              # 等待不算輪數：仍跑滿 2 輪
        self.assertEqual(len(calls), 5)

    def test_command_config_reads_locked_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = cli.Workspace(Path(tmp))
            config.save(ws.root, {"build": "from-file"})
            with patch.dict(os.environ):
                os.environ.pop(parties.LOCKED_ENV, None)
                self.assertEqual(cli.command_config(ws)["build"], "from-file")
                os.environ[parties.LOCKED_ENV] = json.dumps({"build": "locked", "check": None})
                self.assertEqual(cli.command_config(ws), {"build": "locked", "check": None})
                os.environ[parties.LOCKED_ENV] = "not json"
                with self.assertRaises(SystemExit):
                    cli.command_config(ws)

    def test_run_passes_env_only_when_given(self):
        script = "import json,os\nprint(json.dumps({'locked': os.getenv('AGORA_LOCKED_COMMANDS'), " \
                 "'invoked': os.getenv('AGORA_INVOKED')}))\n"
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {parties.LOCKED_ENV: "stale"}):
            events, _ = parties._run([sys.executable, "-c", script], Path(tmp), "", 30)
            self.assertEqual(events, [{"locked": None, "invoked": "1"}])      # 外層殘留的不往下傳
            events, _ = parties._run([sys.executable, "-c", script], Path(tmp), "", 30, None, {parties.LOCKED_ENV: "x"})
            self.assertEqual(events, [{"locked": "x", "invoked": "1"}])

    def test_work_preamble_mentions_build_only_when_configured(self):
        fmt = dict(me="Codex", owner="Claude", human="Richard", workspace="W", progress="P", control="C",
                   threshold=5, agora="agora")
        text = cli.WORK_PREAMBLE.format(**fmt, build_cmd="", finish_cmds="`agora check`")
        self.assertNotIn("agora build", text)


if __name__ == "__main__":
    unittest.main()
