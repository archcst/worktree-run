"""Opt-in real pi smoke test. Requires configured model authentication."""

import os
import shutil
import sys
import unittest

from support import ISSUE, Sandbox

from worktree_run import prompt, runner
from worktree_run.linear import Linear
from worktree_run.tmux import Tmux


@unittest.skipUnless(
    os.environ.get("WTR_TEST_PI") == "1" and shutil.which("pi"),
    "set WTR_TEST_PI=1 to test authenticated pi",
)
class PiSmokeTest(Sandbox):
    def test_real_interactive_initial_prompt(self):
        prompt.initialize("ws-uuid", "project-1").write_text(
            "这是隔离的 CLI 启动测试，{{issue_id}} 是测试标识，不要开发或查询真实任务。\n"
            "请将 WTR_PI_ 与 OK_ENG-123 拼接成一个字符串，仅回复拼接结果。"
        )
        executable = shutil.which("pi")
        command = [
            executable,
            "--offline",
            "--no-extensions",
            "--no-skills",
            "--no-prompt-templates",
            "--no-context-files",
            "--no-tools",
            "--no-session",
            "--no-approve",
        ]
        wrapper = self.base / "pi"
        wrapper.write_text(
            f"#!{sys.executable}\nimport os, sys\nos.execv({executable!r}, {command!r} + sys.argv[1:])\n"
        )
        wrapper.chmod(0o755)
        linear = Linear()
        with self.store.lock():
            run = runner.launch(self.store, linear, ISSUE, self.binding, "pi")
        tmux = Tmux()
        tmux.call("resize-window", "-t", run["window_id"], "-x", "150", "-y", "45")
        self.wait_for(
            lambda: (
                "WTR_PI_OK_ENG-123"
                in tmux.call("capture-pane", "-p", "-S", "-", "-t", run["pane_id"]).stdout
            ),
            timeout=90,
        )
        tmux.call("send-keys", "-t", run["pane_id"], "/quit", "Enter")
        self.wait_for(lambda: self.store.run(run["id"])["status"] == "awaiting_review", timeout=15)
        self.assertEqual(self.store.run(run["id"])["exit_code"], 0)
