import io
import json
import shlex
import shutil
import sys
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from support import ISSUE, Sandbox

from worktree_run import agents, runner
from worktree_run.cli import main, selected_agent
from worktree_run.tmux import Tmux
from worktree_run.util import Error


class AgentTests(Sandbox):
    def context(self):
        return SimpleNamespace(workspace="ws-uuid", slug="test-space")

    def test_builtin_interactive_argument_boundaries(self):
        text = "Linear workspace: test\nENG-123 中文 ' \" $(touch BAD)\n" + "长" * 20000
        commands = {
            "pi": ["pi"],
            "codex": ["codex"],
            "claude": ["claude"],
            "opencode": ["opencode", "--prompt"],
            "gemini": ["gemini", "--prompt-interactive"],
        }
        self.assertEqual(set(agents.BUILTINS), set(commands))
        for name, command in commands.items():
            with self.subTest(agent=name):
                self.assertEqual(agents.get(name).argv(text), [*command, text])

    def test_catalog_includes_installation_and_default_status(self):
        self.store.set_setting("agent", "pi")
        with patch(
            "worktree_run.agents.shutil.which",
            side_effect=lambda cmd: "/tools/pi" if cmd == "pi" else None,
        ):
            rows = agents.catalog(self.store.setting("agent"))
        self.assertEqual([r["name"] for r in rows if r["installed"]], ["pi"])
        self.assertEqual([r["name"] for r in rows if r["default"]], ["pi"])
        self.assertIsNone(self.store.one("SELECT name FROM sqlite_master WHERE name='agents'"))
        result = self.cli("agent", "list")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual({r["name"] for r in json.loads(result.stdout)}, set(agents.BUILTINS))
        self.assertEqual(self.cli("agent").returncode, 0)

    def test_default_can_be_set_without_registering_agent(self):
        result = self.cli("config", "set", "agent", "codex")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(selected_agent(self.store, None), "codex")
        self.assertEqual(selected_agent(self.store, "pi"), "pi")
        self.assertEqual(self.store.setting("agent"), "codex")
        result = self.cli("config", "set", "agent", "unknown")
        self.assertEqual(result.returncode, 1)
        self.assertIn("未知 agent", result.stderr)
        self.assertEqual(self.store.setting("agent"), "codex")

    def test_interactive_choice_only_lists_installed_agents_and_is_not_saved(self):
        with (
            patch("sys.stdin.isatty", return_value=True),
            patch(
                "worktree_run.agents.shutil.which",
                side_effect=lambda cmd: "/tools/pi" if cmd == "pi" else None,
            ),
            patch("worktree_run.cli.choose", return_value={"name": "pi"}) as choose,
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(selected_agent(self.store, None), "pi")
        self.assertEqual([r["name"] for r in choose.call_args.args[0]], ["pi"])
        self.assertIsNone(self.store.setting("agent"))

    def test_explicit_picker_overrides_default_for_one_run(self):
        self.store.set_setting("agent", "codex")
        self.install_agent()
        with (
            patch("sys.stdin.isatty", return_value=True),
            patch("worktree_run.cli.choose", return_value={"name": "pi"}),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(selected_agent(self.store, ""), "pi")
        self.assertEqual(self.store.setting("agent"), "codex")

    def test_noninteractive_launch_requires_choice_or_default(self):
        self.install_agent()
        result = self.cli("ENG-123", "--no-attach")
        self.assertEqual(result.returncode, 1)
        self.assertIn("--agent", result.stderr)
        self.assertEqual(self.store.all("SELECT * FROM runs"), [])
        self.assertFalse((self.base / "trees").exists())

    def test_no_installed_agent_is_actionable(self):
        with patch("worktree_run.agents.shutil.which", return_value=None):
            with self.assertRaisesRegex(Error, "没有已安装"):
                selected_agent(self.store, None)

    def test_unknown_saved_default_is_actionable_and_preserved(self):
        self.store.set_setting("agent", "custom-tool")
        with self.assertRaisesRegex(Error, "wtr config set agent"):
            selected_agent(self.store, None)
        self.assertEqual(self.store.setting("agent"), "custom-tool")
        self.assertEqual(selected_agent(self.store, "pi"), "pi")

    def test_missing_executable_fails_before_creating_worktree(self):
        which = shutil.which
        with patch(
            "worktree_run.agents.shutil.which",
            side_effect=lambda cmd: None if cmd == "opencode" else which(cmd),
        ):
            with self.store.lock(), self.assertRaisesRegex(Error, "opencode 未安装"):
                runner.launch(self.store, self.context(), ISSUE, self.binding, "opencode")
        self.assertFalse((self.base / "trees").exists())
        self.assertEqual(self.store.all("SELECT * FROM runs"), [])
        self.assertEqual(self.store.all("SELECT * FROM issues"), [])

    def test_explicit_agent_dry_run_does_not_write_defaults(self):
        self.install_agent()
        before = self.store.path.read_bytes()
        result = self.cli("ENG-123", "--agent", "pi", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["agent"], "pi")
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertIsNone(self.store.setting("agent"))
        self.assertFalse((self.base / "trees").exists())

    def test_cancel_picker_creates_no_execution(self):
        self.install_agent()
        with (
            patch("sys.stdin.isatty", return_value=True),
            patch("worktree_run.cli.choose", side_effect=KeyboardInterrupt),
            redirect_stdout(io.StringIO()),
        ):
            result = main(["--db", str(self.store.path), "ENG-123", "--agent"])
        self.assertEqual(result, 130)
        self.assertEqual(self.store.all("SELECT * FROM runs"), [])
        self.assertEqual(self.store.all("SELECT * FROM issues"), [])

    def test_legacy_agent_rows_do_not_override_builtin_commands(self):
        self.install_agent()
        self.store.write(
            "CREATE TABLE agents (name TEXT PRIMARY KEY, interactive TEXT, automatic TEXT)"
        )
        self.store.write(
            "INSERT INTO agents VALUES ('pi',?,NULL)", ('["custom-pi", "{{prompt}}"]',)
        )
        self.store.set_setting("agent", "pi")
        before = self.store.all("SELECT * FROM agents")
        _, _, argv, _ = runner.plan(self.store, self.context(), ISSUE, self.binding, "pi")
        self.assertEqual(Path(argv[0]), self.base / "pi")
        self.assertEqual(self.store.all("SELECT * FROM agents"), before)
        self.assertEqual(self.store.setting("agent"), "pi")

    def test_real_interactive_picker_and_explicit_override(self):
        self.install_agent()
        tmux = Tmux()
        for index, force_picker in enumerate((False, True)):
            with self.subTest(force_picker=force_picker):
                if force_picker:
                    self.store.set_setting("agent", "codex")
                command = [
                    sys.executable,
                    "-m",
                    "worktree_run",
                    "--db",
                    str(self.store.path),
                    "ENG-123",
                    "--no-attach",
                ]
                if force_picker:
                    command.append("--agent")
                pane = tmux.call(
                    "new-session",
                    "-d",
                    "-P",
                    "-F",
                    "#{pane_id}",
                    "-s",
                    f"agent-picker-{index}",
                    "-x",
                    "180",
                    "-y",
                    "35",
                    shlex.join(command),
                ).stdout.strip()
                tmux.call("set-option", "-w", "-t", pane, "remain-on-exit", "on")
                self.wait_for(
                    lambda: (
                        "输入序号或完整名称/ID"
                        in tmux.call("capture-pane", "-p", "-t", pane).stdout
                    )
                )
                tmux.call("send-keys", "-t", pane, "pi", "Enter")
                self.wait_for(lambda: len(self.store.all("SELECT * FROM runs")) == index + 1)
                run = self.store.one("SELECT * FROM runs ORDER BY created_at DESC LIMIT 1")
                self.wait_for(lambda: self.store.run(run["id"])["status"] == "awaiting_review")
                self.assertEqual(run["agent"], "pi")
                self.assertEqual(self.store.setting("agent"), "codex" if force_picker else None)

    def test_all_builtin_adapters_launch_in_tmux(self):
        for name, agent in agents.BUILTINS.items():
            with self.subTest(agent=name):
                output = self.install_agent(name)
                with self.store.lock():
                    run = runner.launch(self.store, self.context(), ISSUE, self.binding, name)
                self.wait_for(lambda: self.store.run(run["id"])["status"] == "awaiting_review")
                data = json.loads(output.read_text())
                self.assertEqual(data["argv"], json.loads(run["argv"])[1:])
                self.assertEqual(data["argv"][:-1], list(agent.command[1:]))
                self.assertIn("ENG-123", data["argv"][-1])
                self.assertTrue(data["tty"])
                self.assertEqual(data["cwd"], run["cwd"])
