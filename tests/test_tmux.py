import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from support import ISSUE, Sandbox

from worktree_run import prompt, runner
from worktree_run.linear import Linear
from worktree_run.tmux import Tmux


class TmuxTests(Sandbox):
    def configure(self, delay="0", code="0"):
        self.output_file = self.install_agent(delay=delay, code=code)
        self.store.set_setting("agent", "pi")

    def launch(self):
        linear = Linear()
        with self.store.lock():
            return runner.launch(self.store, linear, ISSUE, self.binding, "pi")

    def test_exit_retention_prompt_cwd_and_rerun(self):
        self.configure()
        template = prompt.initialize("ws-uuid", "project-1")
        template.write_text(
            "{{issue_id}}\n'中文' \"space\" $(touch BAD) ; `false`\n" + "文" * 20000
        )
        run = self.launch()
        finished = self.wait_for(
            lambda: r if (r := self.store.run(run["id"]))["status"] == "awaiting_review" else None
        )
        data = json.loads(self.output_file.read_text())
        self.assertEqual(data["cwd"], run["cwd"])
        self.assertTrue(data["tty"])
        self.assertEqual(data["workspace"], "test-space")
        self.assertEqual(data["argv"][-1], json.loads(run["argv"])[-1])
        self.assertEqual(finished["exit_code"], 0)
        self.assertFalse((Path(run["cwd"]) / "BAD").exists())
        tmux = Tmux()
        self.wait_for(lambda: tmux.locate(self.store, finished)["dead"] == "1")
        capture = tmux.call("capture-pane", "-p", "-t", run["pane_id"]).stdout
        self.assertIn("AGENT FINISHED", capture)
        second = self.launch()
        self.assertNotEqual(second["id"], run["id"])
        self.assertEqual(second["cwd"], run["cwd"])
        self.assertNotEqual(second["pane_id"], run["pane_id"])
        self.wait_for(lambda: self.store.run(second["id"])["status"] == "awaiting_review")

    def test_concurrent_launch_and_renamed_window_deduplicate(self):
        self.configure(delay="30")
        command = [
            sys.executable,
            "-m",
            "worktree_run",
            "--db",
            str(self.store.path),
            "ENG-123",
            "--no-attach",
        ]
        first = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        second = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        for proc in (first, second):
            out, err = proc.communicate(timeout=20)
            self.assertEqual(proc.returncode, 0, out + err)
        self.wait_for(lambda: self.output_file.exists())
        runs = self.store.all("SELECT * FROM runs")
        self.assertEqual(len(runs), 1)
        run = runs[0]
        Tmux().call("rename-window", "-t", run["window_id"], "user-renamed")
        result = self.cli("ENG-123", "--no-attach", "--agent", "does-not-exist")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.store.all("SELECT * FROM runs")), 1)
        Tmux().call("kill-window", "-t", run["window_id"])
        with self.store.lock():
            runner.recover(self.store)
        # HUP may reach the wrapper before the pane disappears; in that case its
        # real nonzero exit record takes precedence over inferred interruption.
        self.assertIn(self.store.run(run["id"])["status"], ("interrupted", "failed"))
        self.assertIsNone(self.store.active("ws-uuid", ISSUE["id"]))
        self.configure()
        result = self.cli("ENG-123", "--no-attach")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.store.all("SELECT * FROM runs")), 2)
        latest = self.store.one("SELECT * FROM runs ORDER BY created_at DESC LIMIT 1")
        self.wait_for(lambda: self.store.run(latest["id"])["status"] == "awaiting_review")

    def test_nonzero_exit(self):
        self.configure(code="7")
        run = self.launch()
        self.wait_for(lambda: self.store.run(run["id"])["status"] == "failed")
        self.assertEqual(self.store.run(run["id"])["exit_code"], 7)

    def test_missing_pane_ids_recovered_from_token(self):
        self.configure(delay="30")
        run = self.launch()
        self.wait_for(lambda: self.output_file.exists())
        self.store.update_run(run["id"], pane_id=None, window_id=None, session_id=None)
        with self.store.lock():
            runner.recover(self.store)
        recovered = self.store.run(run["id"])
        self.assertEqual(recovered["pane_id"], run["pane_id"])
        self.assertEqual(recovered["status"], "running")

    def test_launch_command_recovers_untagged_pane(self):
        self.configure(delay="30")
        run = self.launch()
        self.wait_for(lambda: self.output_file.exists())
        tmux = Tmux()
        for key in ("@wtr_run", "@wtr_store"):
            tmux.call("set-option", "-pu", "-t", run["pane_id"], key)
        self.store.update_run(run["id"], pane_id=None, window_id=None, session_id=None)
        with self.store.lock():
            runner.recover(self.store)
        self.assertEqual(self.store.run(run["id"])["pane_id"], run["pane_id"])
        self.assertEqual(self.store.run(run["id"])["status"], "running")

    def test_repo_cannot_shadow_internal_module(self):
        self.configure()
        from worktree_run.worktree import git

        (self.repo / "worktree_run.py").write_text("raise RuntimeError('shadowed module')")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "add shadow module")
        run = self.launch()
        self.wait_for(lambda: self.store.run(run["id"])["status"] == "awaiting_review")

    def test_real_fzf_preview_cancel_and_selection(self):
        self.configure()
        tmux = Tmux()
        command = shlex.join(
            [sys.executable, "-m", "worktree_run", "--db", str(self.store.path), "--no-attach"]
        )
        pane = tmux.call(
            "new-session",
            "-d",
            "-P",
            "-F",
            "#{pane_id}",
            "-s",
            "picker",
            "-x",
            "150",
            "-y",
            "40",
            command,
        ).stdout.strip()
        tmux.call("set-option", "-w", "-t", pane, "remain-on-exit", "on")
        self.wait_for(lambda: "详情" in tmux.call("capture-pane", "-p", "-t", pane).stdout)
        tmux.call("send-keys", "-t", pane, "Escape")
        self.wait_for(
            lambda: (
                tmux.call("display-message", "-p", "-t", pane, "#{pane_dead}").stdout.strip() == "1"
            )
        )
        self.assertEqual(self.store.all("SELECT * FROM runs"), [])
        self.assertFalse((self.base / "trees").exists())
        self.assertFalse(prompt.prompt_path("ws-uuid", "project-1").exists())
        tmux.call("respawn-pane", "-t", pane, command)
        self.wait_for(lambda: "详情" in tmux.call("capture-pane", "-p", "-t", pane).stdout)
        tmux.call("send-keys", "-t", pane, "Enter")
        self.wait_for(lambda: self.output_file.exists())
        run = self.store.one("SELECT * FROM runs")
        self.wait_for(lambda: self.store.run(run["id"])["status"] == "awaiting_review")

    def test_noninteractive_launch_does_not_attach(self):
        self.configure()
        result = self.cli("ENG-123")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("后台窗口已保留", result.stdout)
        run = self.store.one("SELECT * FROM runs")
        self.wait_for(lambda: self.store.run(run["id"])["status"] == "awaiting_review")

    def test_focus_inside_and_outside_tmux(self):
        tmux = Tmux()
        run = dict(id="run", session_id="$0", window_id="@1", pane_id="%1")
        with (
            patch("sys.stdin.isatty", return_value=True),
            patch("sys.stdout.isatty", return_value=True),
            patch.object(tmux, "call") as call,
        ):
            with patch.dict(os.environ, {"TMUX": tmux.socket + ",123,0"}):
                tmux.focus(run)
                call.assert_any_call("switch-client", "-t", "$0:@1")
            with (
                patch.dict(os.environ, {"TMUX": ""}),
                patch("worktree_run.tmux.subprocess.run") as attach,
            ):
                attach.return_value.returncode = 0
                tmux.focus(run)
                self.assertIn("attach-session", attach.call_args.args[0])
