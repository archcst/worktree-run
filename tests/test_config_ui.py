import fcntl
import json
import os
import shlex
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from support import ISSUE, Sandbox

from worktree_run import config_ui, prompt, worktree
from worktree_run.tmux import Tmux
from worktree_run.util import Error


class ConfigMenuTests(Sandbox):
    def assert_unlocked(self):
        with self.store.path.with_suffix(".lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(handle, fcntl.LOCK_UN)

    def test_config_without_tty_is_actionable_and_scripts_still_work(self):
        result = self.cli("config")
        self.assertEqual(result.returncode, 1)
        self.assertIn("需要交互终端", result.stderr)
        self.assertEqual(self.cli("config", "set", "agent", "pi").returncode, 0)
        self.assertEqual(json.loads(self.cli("config", "list").stdout)["agent"], "pi")

    def test_exit_menu_is_local_and_does_not_hold_lock(self):
        def exit_menu(*args, **kwargs):
            self.assert_unlocked()
            self.assertEqual(
                [key for key, _ in args[1]],
                ["bindings", "prompt", "agent", "tmux", "workspace", "overview", None],
            )
            return None

        before = self.store.bindings()
        with (
            patch("sys.stdin.isatty", return_value=True),
            patch("sys.stdout.isatty", return_value=True),
            patch.object(config_ui, "menu", side_effect=exit_menu),
            patch.object(config_ui, "Linear") as linear,
        ):
            config_ui.run(self.store)
        linear.assert_not_called()
        self.assertEqual(self.store.bindings(), before)
        self.assertEqual(self.store.all("SELECT * FROM settings"), [])
        self.assertFalse(prompt.directory().exists())

    def test_set_and_clear_default_agent(self):
        self.install_agent()
        with patch.object(config_ui, "menu", return_value="pi"), patch.object(config_ui, "notice"):
            config_ui.set_default_agent(self.store)
        self.assertEqual(self.store.setting("agent"), "pi")
        with patch.object(config_ui, "menu", return_value="ask"), patch.object(config_ui, "notice"):
            config_ui.set_default_agent(self.store)
        self.assertIsNone(self.store.setting("agent"))

    def test_tmux_validation_retries_and_cancel_preserves_value(self):
        self.store.set_setting("tmux-session", "original")
        with (
            patch.object(config_ui, "input_text", side_effect=["bad:name", None]),
            patch.object(config_ui, "notice") as notice,
        ):
            config_ui.set_tmux_session(self.store)
        self.assertEqual(self.store.setting("tmux-session"), "original")
        self.assertEqual(notice.call_args.args[0], "输入无效")
        with (
            patch.object(config_ui, "input_text", return_value="development"),
            patch.object(config_ui, "notice"),
        ):
            config_ui.set_tmux_session(self.store)
        self.assertEqual(self.store.setting("tmux-session"), "development")

    def test_add_binding_uses_current_repo_and_defers_writes_until_confirmed(self):
        self.store.remove_binding("ws-uuid", "project", "project-1")
        self.store.write("DELETE FROM prompt_projects")
        answers = iter(["main", None])

        def answer(*args, **kwargs):
            self.assert_unlocked()
            return next(answers)

        with (
            patch.object(config_ui.Path, "cwd", return_value=self.repo),
            patch.object(
                config_ui, "menu", side_effect=["project", {"id": "project-1", "name": "Project"}]
            ),
            patch.object(config_ui, "input_text", side_effect=answer),
        ):
            self.assertIsNone(config_ui.add_binding(self.store, None))
        self.assertEqual(self.store.bindings(), [])
        self.assertFalse(prompt.directory().exists())
        with (
            patch.object(config_ui.Path, "cwd", return_value=self.repo),
            patch.object(
                config_ui, "menu", side_effect=["project", {"id": "project-1", "name": "Project"}]
            ),
            patch.object(config_ui, "input_text", side_effect=["main", str(self.repo)]),
        ):
            self.assertEqual(
                config_ui.add_binding(self.store, None), ("ws-uuid", "project", "project-1")
            )
        row = self.store.binding("ws-uuid", "project", "project-1")
        self.assertEqual(row["repo"], str(self.repo.resolve()))
        self.assertEqual(row["root"], str(self.repo.resolve()))
        self.assertIsNotNone(self.store.project("ws-uuid", "project-1"))
        self.assertFalse(prompt.directory().exists())

    def test_add_requires_main_root_before_querying_linear(self):
        with (
            patch.object(config_ui.Path, "cwd", return_value=self.base),
            patch.object(config_ui, "Linear") as linear,
        ):
            with self.assertRaisesRegex(Error, "wtr config"):
                config_ui.add_binding(self.store, None)
        linear.assert_not_called()

    def test_edit_binding_prefills_values_and_preserves_template(self):
        template = prompt.initialize("ws-uuid", "project-1")
        template.write_text("Keep {{issue_id}}")
        with (
            patch.object(config_ui, "menu", side_effect=["baseline", "root", None]),
            patch.object(
                config_ui, "input_text", side_effect=["HEAD", str(self.base / "新目录")]
            ) as text,
        ):
            config_ui.binding_settings(self.store, ("ws-uuid", "project", "project-1"))
        self.assertEqual(text.call_args_list[0].args[1], "main")
        self.assertEqual(text.call_args_list[1].args[1], self.binding["root"])
        row = self.store.binding("ws-uuid", "project", "project-1")
        self.assertEqual(row["baseline"], "HEAD")
        self.assertEqual(row["root"], str((self.base / "新目录").resolve()))
        self.assertFalse((self.base / "新目录").exists())
        self.assertEqual(template.read_text(), "Keep {{issue_id}}")

    def test_concurrent_binding_update_is_not_overwritten(self):
        original = self.store.binding("ws-uuid", "project", "project-1")
        self.store.save_binding(dict(original, baseline="HEAD"))
        with self.assertRaisesRegex(Error, "其他进程修改"):
            config_ui.save_binding(
                self.store, dict(original, root=str(self.repo)), expected=original
            )
        self.assertEqual(self.store.binding("ws-uuid", "project", "project-1")["baseline"], "HEAD")

    def test_remove_requires_confirmation_and_preserves_worktree_and_prompt(self):
        context = SimpleNamespace(workspace="ws-uuid", slug="test-space")
        row = worktree.route(self.store, context, ISSUE, self.binding)
        self.store.save_issue(row)
        worktree.prepare(row, associated=False)
        template = prompt.initialize("ws-uuid", "project-1")
        with patch.object(config_ui, "menu", side_effect=["remove", False, None]) as menu:
            config_ui.binding_settings(self.store, ("ws-uuid", "project", "project-1"))
        self.assertIsNotNone(self.store.binding("ws-uuid", "project", "project-1"))
        self.assertIs(menu.call_args_list[1].kwargs["default"], False)
        with patch.object(config_ui, "menu", side_effect=["remove", True]):
            config_ui.binding_settings(self.store, ("ws-uuid", "project", "project-1"))
        self.assertIsNone(self.store.binding("ws-uuid", "project", "project-1"))
        self.assertIsNotNone(self.store.project("ws-uuid", "project-1"))
        self.assertTrue(template.exists())
        self.assertTrue(Path(row["worktree"]).is_dir())
        self.assertIsNotNone(self.store.issue("ws-uuid", ISSUE["id"]))

    def test_project_prompt_selection_honors_workspace_and_does_not_hold_lock(self):
        other = dict(
            workspace="other-ws",
            workspace_slug="other-space",
            project_id="project-1",
            name="Project",
        )
        self.store.save_project(other)

        def edit(row):
            self.assertEqual(row, other)
            self.assert_unlocked()
            return prompt.initialize(row["workspace"], row["project_id"])

        with (
            patch("sys.stdin.isatty", return_value=True),
            patch("sys.stdout.isatty", return_value=True),
            patch.object(
                config_ui, "menu", side_effect=["prompt", ("other-ws", "project-1"), None]
            ) as menu,
            patch.object(config_ui.prompt, "edit", side_effect=edit),
            patch.object(config_ui, "notice"),
            patch.object(config_ui, "Linear") as linear,
        ):
            config_ui.run(self.store, "other-space")
        linear.assert_not_called()
        self.assertEqual(
            [key for key, _ in menu.call_args_list[1].args[1]],
            [("other-ws", "project-1"), "other", None],
        )
        self.assertFalse(prompt.prompt_path("ws-uuid", "project-1").exists())

    def test_binding_errors_return_to_menu(self):
        with (
            patch.object(config_ui.Path, "cwd", return_value=self.base),
            patch.object(config_ui, "menu", side_effect=["add", None]),
            patch.object(config_ui, "notice") as notice,
        ):
            config_ui.manage_bindings(self.store, None)
        self.assertEqual(notice.call_args.args[0], "操作失败")
        self.assertIn("main worktree", notice.call_args.args[1])

    def test_workspace_selection_is_session_local(self):
        before = self.store.all("SELECT * FROM settings")
        with (
            patch.object(config_ui, "menu", return_value=("input", None)),
            patch.object(config_ui, "input_text", return_value="another-space"),
        ):
            self.assertEqual(config_ui.select_workspace(self.store, None), "another-space")
        self.assertEqual(self.store.all("SELECT * FROM settings"), before)

    def test_add_binding_type_cancel_is_local_and_does_not_write(self):
        before = self.store.bindings()
        with (
            patch.object(config_ui.Path, "cwd", return_value=self.repo),
            patch.object(config_ui, "menu", return_value=None),
            patch.object(config_ui, "Linear") as linear,
            patch.object(config_ui, "input_text") as text,
        ):
            self.assertIsNone(config_ui.add_binding(self.store, None))
        linear.assert_not_called()
        text.assert_not_called()
        self.assertEqual(self.store.bindings(), before)
        self.assertFalse(prompt.directory().exists())

    def test_add_label_caches_no_project_and_creates_no_template(self):
        projects = self.store.projects()
        target = {"id": "label-1", "name": "repo:app", "team": None}
        with (
            patch.object(config_ui.Path, "cwd", return_value=self.repo),
            patch.object(config_ui, "menu", side_effect=["label", target]),
            patch.object(config_ui, "input_text", side_effect=["main", str(self.repo)]),
        ):
            self.assertEqual(
                config_ui.add_binding(self.store, None), ("ws-uuid", "label", "label-1")
            )
        row = self.store.binding("ws-uuid", "label", "label-1")
        self.assertEqual(row["name"], "repo:app")
        self.assertEqual(row["team_name"], "")
        self.assertEqual(row["repo"], str(self.repo.resolve()))
        self.assertEqual(row["baseline"], "main")
        self.assertEqual(row["root"], str(self.repo.resolve()))
        self.assertEqual(self.store.projects(), projects)
        self.assertFalse(prompt.directory().exists())

    def test_label_scope_names_and_ids_are_searchable_and_selection_uses_object(self):
        targets = [
            {"id": "label-global", "name": "repo:app", "team": None},
            {
                "id": "label-team",
                "name": "repo:app",
                "team": {"id": "team-1", "name": "Engineering", "key": "ENG"},
            },
        ]
        with (
            patch.object(config_ui.Path, "cwd", return_value=self.repo),
            patch.object(config_ui, "Linear") as linear,
            patch.object(config_ui, "menu", side_effect=["label", targets[1]]) as menu,
            patch.object(config_ui, "input_text", side_effect=["main", str(self.repo)]),
        ):
            client = linear.return_value
            client.workspace, client.slug = "ws-uuid", "test-space"

            def labels():
                self.assert_unlocked()
                return targets

            client.labels.side_effect = labels
            config_ui.add_binding(self.store, "test-space")
        client.projects.assert_not_called()
        choices = menu.call_args_list[1].args[1][:-1]
        self.assertEqual([value for value, _ in choices], targets)
        for target, label in choices:
            self.assertIn(target["id"], label)
            self.assertIn("repo:app", label)
        self.assertIn("Engineering", choices[1][1])
        self.assertNotEqual(choices[0][1], choices[1][1])
        self.assertTrue(menu.call_args_list[1].kwargs["search"])
        row = self.store.binding("ws-uuid", "label", "label-team")
        self.assertEqual(row["team_name"], "Engineering (ENG)")
        self.assertIsNone(self.store.binding("ws-uuid", "label", "label-global"))

    def test_add_filters_bound_objects_by_workspace_kind_and_id(self):
        targets = [
            {"id": "project-1", "name": "Same name", "team": None},
            {"id": "label-bound", "name": "Same name", "team": None},
        ]
        self.store.save_binding(
            dict(self.binding, kind="label", target_id="label-bound", name="Same name")
        )
        with (
            patch.object(config_ui.Path, "cwd", return_value=self.repo),
            patch.object(config_ui, "Linear") as linear,
            patch.object(config_ui, "menu", side_effect=["label", None]) as menu,
            patch.object(config_ui, "input_text") as text,
        ):
            client = linear.return_value
            client.workspace, client.slug = "ws-uuid", "test-space"
            client.labels.return_value = targets
            self.assertIsNone(config_ui.add_binding(self.store, None))
        self.assertEqual([value for value, _ in menu.call_args.args[1]], [targets[0], None])
        text.assert_not_called()

    def test_binding_choices_include_type_scope_workspace_and_id(self):
        rows = [
            self.binding,
            dict(self.binding, kind="label", target_id="label-1", team_name="Engineering"),
            dict(self.binding, workspace="other-ws", workspace_slug="other-space"),
        ]
        choices = config_ui.binding_choices(rows)
        self.assertEqual(len(set(key for key, _ in choices)), 3)
        self.assertEqual(len(set(label for _, label in choices)), 3)
        for row, (key, label) in zip(rows, choices):
            self.assertEqual(key, (row["workspace"], row["kind"], row["target_id"]))
            self.assertIn(row["name"], label)
            self.assertIn(row["workspace_slug"], label)
            self.assertIn(row["target_id"], label)
        self.assertIn("Engineering", choices[1][1])

    def test_binding_settings_contains_only_binding_actions(self):
        with patch.object(config_ui, "menu", return_value=None) as menu:
            config_ui.binding_settings(self.store, ("ws-uuid", "project", "project-1"))
        self.assertEqual(
            [key for key, _ in menu.call_args.args[1]],
            ["target", "baseline", "root", "repo", "remove", None],
        )

    def test_binding_field_cancellation_preserves_row(self):
        original = self.store.binding("ws-uuid", "project", "project-1")
        with (
            patch.object(config_ui, "menu", side_effect=["baseline", "root", None]),
            patch.object(config_ui, "input_text", return_value=None),
        ):
            config_ui.binding_settings(self.store, ("ws-uuid", "project", "project-1"))
        self.assertEqual(self.store.binding("ws-uuid", "project", "project-1"), original)

    def test_rebind_requires_confirmation_and_baseline_then_saves(self):
        original = self.store.binding("ws-uuid", "project", "project-1")
        self.store.save_binding(dict(original, repo=str(self.base / "old-repo")))
        for confirmed, baseline in [(False, None), (True, None), (True, "main")]:
            with (
                self.subTest(confirmed=confirmed, baseline=baseline),
                patch.object(config_ui.Path, "cwd", return_value=self.repo),
                patch.object(config_ui, "menu", side_effect=["repo", confirmed, None]),
                patch.object(config_ui, "input_text", return_value=baseline) as text,
            ):
                config_ui.binding_settings(self.store, ("ws-uuid", "project", "project-1"))
                row = self.store.binding("ws-uuid", "project", "project-1")
                self.assertEqual(
                    row["repo"],
                    str(self.repo.resolve()) if baseline else str(self.base / "old-repo"),
                )
                self.assertEqual(row["root"], original["root"])
                if not confirmed:
                    text.assert_not_called()

    def test_concurrent_remove_preserves_updated_binding(self):
        def choose(title, choices, **kwargs):
            self.assert_unlocked()
            if title == "确认解除绑定":
                self.store.save_binding(dict(self.binding, baseline="HEAD"))
                return True
            return (
                "remove"
                if self.store.binding("ws-uuid", "project", "project-1")["baseline"] == "main"
                else None
            )

        with (
            patch.object(config_ui, "menu", side_effect=choose),
            patch.object(config_ui, "notice") as notice,
        ):
            config_ui.binding_settings(self.store, ("ws-uuid", "project", "project-1"))
        self.assertIn("其他进程修改", notice.call_args.args[1])
        self.assertEqual(self.store.binding("ws-uuid", "project", "project-1")["baseline"], "HEAD")

    def test_prompt_remote_selection_without_bindings_caches_identity_before_editor(self):
        self.store.remove_binding("ws-uuid", "project", "project-1")
        self.store.write("DELETE FROM prompt_projects")
        target = {"id": "project-1", "name": "Project"}

        def edit(row):
            self.assert_unlocked()
            self.assertEqual(row, self.store.project("ws-uuid", "project-1"))
            self.assertEqual(set(row), {"workspace", "workspace_slug", "project_id", "name"})
            self.assertEqual(self.store.bindings(), [])
            self.assertFalse(prompt.directory().exists())
            return prompt.initialize(row["workspace"], row["project_id"])

        with (
            patch.object(config_ui, "menu", side_effect=["other", target]) as menu,
            patch.object(config_ui.prompt, "edit", side_effect=edit),
            patch.object(config_ui, "notice"),
        ):
            config_ui.edit_project_prompt(self.store, None)
        self.assertEqual([value for value, _ in menu.call_args_list[0].args[1]], ["other", None])
        self.assertEqual(menu.call_args_list[1].args[1][0][0], target)
        self.assertEqual(prompt.prompt_path("ws-uuid", "project-1").read_text(), prompt.DEFAULT)
        self.assertEqual(self.store.bindings(), [])

    def test_prompt_remote_selection_cancel_does_not_cache_or_edit(self):
        self.store.write("DELETE FROM prompt_projects")
        with (
            patch.object(config_ui, "menu", side_effect=["other", None]),
            patch.object(config_ui.prompt, "edit") as edit,
        ):
            config_ui.edit_project_prompt(self.store, None)
        edit.assert_not_called()
        self.assertEqual(self.store.projects(), [])
        self.assertFalse(prompt.directory().exists())

    def test_workspace_choices_include_label_and_prompt_only_workspaces(self):
        self.store.save_binding(
            dict(
                self.binding,
                workspace="labels-ws",
                workspace_slug="labels-space",
                kind="label",
                target_id="label-1",
            )
        )
        self.store.save_project(
            dict(
                workspace="prompts-ws",
                workspace_slug="prompts-space",
                project_id="prompt-project",
                name="Prompt only",
            )
        )
        with patch.object(config_ui, "menu", return_value=None) as menu:
            self.assertEqual(
                config_ui.select_workspace(self.store, "current-space"), "current-space"
            )
        self.assertEqual(
            [value for value, _ in menu.call_args.args[1]],
            [
                ("scope", None),
                ("scope", "current-space"),
                ("scope", "labels-space"),
                ("scope", "prompts-space"),
                ("scope", "test-space"),
                ("input", None),
                None,
            ],
        )
        with patch.object(config_ui, "menu", return_value=None) as menu:
            config_ui.manage_bindings(self.store, "labels-space")
        self.assertEqual(
            [value for value, _ in menu.call_args.args[1]],
            ["add", ("labels-ws", "label", "label-1"), None],
        )

    def open_menu(self, cwd=None):
        tmux = Tmux()
        command = shlex.join(
            [sys.executable, "-m", "worktree_run", "--db", str(self.store.path), "config"]
        )
        pane = tmux.call(
            "new-session",
            "-d",
            "-P",
            "-F",
            "#{pane_id}",
            "-s",
            "config-test",
            "-x",
            "240",
            "-y",
            "40",
            "-c",
            str(cwd or self.repo),
            command,
        ).stdout.strip()
        tmux.call("set-option", "-w", "-t", pane, "remain-on-exit", "on")
        self.screen(tmux, pane, "wtr 配置")
        return tmux, pane

    def screen(self, tmux, pane, text):
        self.wait_for(lambda: text in tmux.call("capture-pane", "-p", "-t", pane).stdout)

    def end_menu(self, tmux, pane):
        tmux.call("send-keys", "-t", pane, "Escape")
        self.wait_for(
            lambda: (
                tmux.call("display-message", "-p", "-t", pane, "#{pane_dead}").stdout.strip() == "1"
            )
        )
        self.assertEqual(
            tmux.call("display-message", "-p", "-t", pane, "#{pane_dead_status}").stdout.strip(),
            "0",
        )

    def test_real_terminal_vim_menu_navigation(self):
        tmux, pane = self.open_menu()
        tmux.call("send-keys", "-t", pane, "j", "j", "k", "j", "Enter")
        self.screen(tmux, pane, "每次启动时选择 agent")
        tmux.call("send-keys", "-t", pane, "q")
        self.screen(tmux, pane, "wtr 配置")
        tmux.call("send-keys", "-t", pane, "G")
        self.screen(tmux, pane, "› 7. 退出")
        tmux.call("send-keys", "-t", pane, "g", "g")
        self.screen(tmux, pane, "› 1. 仓库绑定")
        self.assertEqual(self.store.all("SELECT * FROM settings"), [])
        self.end_menu(tmux, pane)

    def test_real_terminal_emacs_menu_navigation(self):
        tmux, pane = self.open_menu()
        tmux.call("send-keys", "-t", pane, "C-n", "C-n", "C-p", "C-n", "Enter")
        self.screen(tmux, pane, "每次启动时选择 agent")
        tmux.call("send-keys", "-t", pane, "Escape", "<")
        self.screen(tmux, pane, "› 1. pi (pi)")
        tmux.call("send-keys", "-t", pane, "Escape", ">")
        self.screen(tmux, pane, "› 7. 返回配置")
        tmux.call("send-keys", "-t", pane, "C-p")
        self.screen(tmux, pane, "› 6. 每次启动时选择 agent")
        tmux.call("send-keys", "-t", pane, "C-g")
        self.screen(tmux, pane, "wtr 配置")
        self.assertEqual(self.store.all("SELECT * FROM settings"), [])
        self.end_menu(tmux, pane)

    def test_real_terminal_search_separates_text_from_vim_keys(self):
        self.store.save_binding(dict(self.binding, name="jkkggGq Project"))
        tmux, pane = self.open_menu()
        tmux.call("send-keys", "-t", pane, "Enter")
        self.screen(tmux, pane, "＋ 绑定当前仓库的 Project 或 Label")
        tmux.call("send-keys", "-l", "-t", pane, "/jkkggGq")
        self.screen(tmux, pane, "搜索：jkkggGq（搜索输入中")
        tmux.call("send-keys", "-t", pane, "C-n")
        self.screen(tmux, pane, "› 返回配置")
        tmux.call("send-keys", "-t", pane, "C-p")
        self.screen(
            tmux,
            pane,
            "› " + config_ui.bindings.describe(dict(self.binding, name="jkkggGq Project")),
        )
        tmux.call("send-keys", "-t", pane, "C-g")
        self.screen(tmux, pane, "搜索：jkkggGq（导航模式")
        tmux.call("send-keys", "-t", pane, "/")
        self.screen(tmux, pane, "搜索：jkkggGq（搜索输入中")
        tmux.call("send-keys", "-t", pane, "Escape")
        self.screen(tmux, pane, "搜索：jkkggGq（导航模式")
        tmux.call("send-keys", "-t", pane, "j")
        self.screen(tmux, pane, "› 返回配置")
        tmux.call("send-keys", "-t", pane, "k")
        self.screen(
            tmux,
            pane,
            "› " + config_ui.bindings.describe(dict(self.binding, name="jkkggGq Project")),
        )
        tmux.call("send-keys", "-t", pane, "G")
        self.screen(tmux, pane, "› 返回配置")
        tmux.call("send-keys", "-t", pane, "g", "g")
        self.screen(
            tmux,
            pane,
            "› " + config_ui.bindings.describe(dict(self.binding, name="jkkggGq Project")),
        )
        self.screen(tmux, pane, "搜索：jkkggGq（导航模式")
        tmux.call("send-keys", "-t", pane, "Enter")
        self.screen(tmux, pane, "绑定设置：jkkggGq Project")
        tmux.call("send-keys", "-t", pane, "q")
        self.screen(tmux, pane, "＋ 绑定当前仓库的 Project 或 Label")
        tmux.call("send-keys", "-t", pane, "q")
        self.screen(tmux, pane, "wtr 配置")
        self.end_menu(tmux, pane)

    def test_real_terminal_arrows_invalid_input_and_cancel(self):
        tmux, pane = self.open_menu()
        tmux.call("send-keys", "-t", pane, "Down", "Down", "Enter")
        self.screen(tmux, pane, "每次启动时选择 agent")
        tmux.call("send-keys", "-t", pane, "Escape")
        self.screen(tmux, pane, "wtr 配置")
        tmux.call("send-keys", "-t", pane, "Down", "Enter")
        self.screen(tmux, pane, "tmux session 名称")
        tmux.call("send-keys", "-t", pane, "C-a", "C-k", "bad:name", "Enter")
        self.screen(tmux, pane, "输入无效")
        tmux.call("send-keys", "-t", pane, "Escape")
        self.screen(tmux, pane, "tmux session 名称（Enter 确认，Esc 取消）")
        tmux.call("send-keys", "-t", pane, "Escape")
        self.screen(tmux, pane, "wtr 配置")
        self.assertEqual(self.store.all("SELECT * FROM settings"), [])
        self.end_menu(tmux, pane)

    def test_real_terminal_project_search_edits_the_selected_workspace(self):
        self.store.save_project(
            dict(
                workspace="other-ws",
                workspace_slug="other-space",
                project_id="project-1",
                name="Project",
            )
        )
        editor = self.base / "editor.py"
        editor.write_text(
            "import sys\nfrom pathlib import Path\nPath(sys.argv[1]).write_text('Other project {{issue_id}}')\n"
        )
        with patch.dict(
            os.environ, {"VISUAL": "", "EDITOR": shlex.join([sys.executable, str(editor)])}
        ):
            tmux, pane = self.open_menu()
            tmux.call("send-keys", "-t", pane, "2", "Enter")
            self.screen(tmux, pane, "选择项目提示词")
            tmux.call("send-keys", "-l", "-t", pane, "other-space")
            self.screen(tmux, pane, "搜索：other-space")
            tmux.call("send-keys", "-t", pane, "Enter")
            self.screen(tmux, pane, "项目提示词已保存")
            self.assertEqual(
                prompt.prompt_path("other-ws", "project-1").read_text(),
                "Other project {{issue_id}}",
            )
            self.assertFalse(prompt.prompt_path("ws-uuid", "project-1").exists())
            tmux.call("send-keys", "-t", pane, "Escape")
            self.screen(tmux, pane, "wtr 配置")
            self.end_menu(tmux, pane)

    def test_real_terminal_changes_project_binding_to_label(self):
        tmux, pane = self.open_menu()
        tmux.call("send-keys", "-t", pane, "Enter")
        self.screen(tmux, pane, "＋ 绑定当前仓库的 Project 或 Label")
        tmux.call("send-keys", "-t", pane, "Down", "Enter")
        self.screen(tmux, pane, "绑定设置：Project")
        tmux.call("send-keys", "-t", pane, "1", "Enter")
        self.screen(tmux, pane, "修改匹配方式")
        tmux.call("send-keys", "-t", pane, "2", "Enter")
        self.screen(tmux, pane, "选择匹配对象")
        tmux.call("send-keys", "-t", pane, "Enter")
        self.screen(tmux, pane, "确认修改匹配方式/对象")
        tmux.call("send-keys", "-t", pane, "2", "Enter")
        self.screen(tmux, pane, "绑定设置：repo:app")
        row = self.store.binding("ws-uuid", "label", "label-1")
        for field in ("repo", "baseline", "root"):
            self.assertEqual(row[field], self.binding[field])
        self.assertIsNone(self.store.binding("ws-uuid", "project", "project-1"))
        tmux.call("send-keys", "-t", pane, "q")
        self.screen(tmux, pane, "＋ 绑定当前仓库的 Project 或 Label")
        tmux.call("send-keys", "-t", pane, "q")
        self.screen(tmux, pane, "wtr 配置")
        self.end_menu(tmux, pane)

    def test_real_terminal_changes_multiple_settings_then_exits(self):
        self.install_agent()
        fixture = json.loads(self.fixture.read_text())
        fixture["fail"] = True
        self.fixture.write_text(json.dumps(fixture))
        tmux, pane = self.open_menu()
        self.assert_unlocked()
        tmux.call("send-keys", "-t", pane, "3", "Enter")
        self.screen(tmux, pane, "每次启动时选择 agent")
        tmux.call("send-keys", "-t", pane, "1", "Enter")
        self.screen(tmux, pane, "默认 agent 已保存")
        self.assertEqual(self.store.setting("agent"), "pi")
        tmux.call("send-keys", "-t", pane, "Escape")
        self.screen(tmux, pane, "wtr 配置")
        tmux.call("send-keys", "-t", pane, "4", "Enter")
        self.screen(tmux, pane, "tmux session 名称")
        tmux.call("send-keys", "-t", pane, "C-a", "C-k", "dev-session", "Enter")
        self.screen(tmux, pane, "tmux 设置已保存")
        self.assertEqual(self.store.setting("tmux-session"), "dev-session")
        tmux.call("send-keys", "-t", pane, "Escape")
        self.screen(tmux, pane, "wtr 配置")
        self.end_menu(tmux, pane)
        self.assertEqual(self.store.all("SELECT * FROM runs"), [])

    def test_real_terminal_binding_and_independent_prompt(self):
        self.store.remove_binding("ws-uuid", "project", "project-1")
        editor = self.base / "editor.py"
        editor.write_text(
            "import sys\nfrom pathlib import Path\nPath(sys.argv[1]).write_text('Menu project {{issue_id}}')\n"
        )
        with patch.dict(
            os.environ, {"VISUAL": "", "EDITOR": shlex.join([sys.executable, str(editor)])}
        ):
            tmux, pane = self.open_menu()
            tmux.call("send-keys", "-t", pane, "Enter")
            self.screen(tmux, pane, "＋ 绑定当前仓库的 Project 或 Label")
            tmux.call("send-keys", "-t", pane, "Enter")
            self.screen(tmux, pane, "选择绑定类型")
            tmux.call("send-keys", "-t", pane, "Enter")
            self.screen(tmux, pane, "Linear 工作区：test-space")
            tmux.call("send-keys", "-t", pane, "Enter")
            self.screen(tmux, pane, "新分支基线")
            tmux.call("send-keys", "-t", pane, "Enter")
            self.screen(tmux, pane, "issue worktree 根目录（Enter")
            tmux.call("send-keys", "-t", pane, "Enter")
            self.screen(tmux, pane, "绑定设置：Project")
            self.assertEqual(
                self.store.binding("ws-uuid", "project", "project-1")["root"],
                str(self.repo.resolve()),
            )
            self.assertFalse(prompt.directory().exists())
            tmux.call("send-keys", "-t", pane, "5", "Enter")
            self.screen(tmux, pane, "确认解除绑定")
            tmux.call("send-keys", "-t", pane, "Enter")
            self.screen(tmux, pane, "绑定设置：Project")
            self.assertIsNotNone(self.store.binding("ws-uuid", "project", "project-1"))
            tmux.call("send-keys", "-t", pane, "5", "Enter")
            self.screen(tmux, pane, "确认解除绑定")
            tmux.call("send-keys", "-t", pane, "2", "Enter")
            self.screen(tmux, pane, "＋ 绑定当前仓库的 Project 或 Label")
            self.assertIsNone(self.store.binding("ws-uuid", "project", "project-1"))
            tmux.call("send-keys", "-t", pane, "Escape")
            self.screen(tmux, pane, "wtr 配置")
            tmux.call("send-keys", "-t", pane, "2", "Enter")
            self.screen(tmux, pane, "选择项目提示词")
            tmux.call("send-keys", "-t", pane, "Enter")
            self.screen(tmux, pane, "项目提示词已保存")
            self.assertEqual(
                prompt.prompt_path("ws-uuid", "project-1").read_text(),
                "Menu project {{issue_id}}",
            )
            self.assertEqual(self.store.bindings(), [])
            tmux.call("send-keys", "-t", pane, "Escape")
            self.screen(tmux, pane, "wtr 配置")
            self.end_menu(tmux, pane)
        self.assertEqual(self.store.all("SELECT * FROM runs"), [])
