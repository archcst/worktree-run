import fcntl
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

from support import ISSUE, Sandbox

from worktree_run import bindings, config_ui, prompt, worktree
from worktree_run.store import Store
from worktree_run.util import Error

LABEL = {"id": "label-1", "name": "repo:app", "team": None}


class BindingChangeTests(Sandbox):
    def label_row(self, **values):
        return bindings.from_target(
            SimpleNamespace(workspace="ws-uuid", slug="test-space"),
            "label",
            LABEL,
            dict(self.binding, **values),
        )

    def test_ui_switch_preserves_local_identity_settings_history_and_prompt(self):
        self.binding = self.store.save_binding(
            dict(
                self.binding,
                name="Local app",
                agent="pi",
                tmux_session="app-session",
            )
        )
        context = SimpleNamespace(workspace="ws-uuid", slug="test-space")
        issue = worktree.route(self.store, context, ISSUE, self.binding)
        self.store.save_issue(issue)
        run_id = self.store.new_run(issue, "pi", ["pi", "prompt"], None)
        self.store.update_run(run_id, status="failed")
        history = self.store.run(run_id)
        template = prompt.initialize("ws-uuid", "wtr-project-1")
        template.write_text("Keep {{issue_id}}")
        answers = iter(["label", LABEL, True])

        def answer(*args, **kwargs):
            with self.store.path.with_suffix(".lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(lock, fcntl.LOCK_UN)
            return next(answers)

        with patch.object(config_ui, "menu", side_effect=answer):
            key = config_ui.change_binding_target(self.store, self.binding)
        self.assertEqual(key, ("ws-uuid", "wtr-project-1"))
        self.assertEqual(self.store.project(*key), self.label_row())
        self.assertIsNone(self.store.binding("ws-uuid", "project", "project-1"))
        self.assertEqual(self.store.issue("ws-uuid", ISSUE["id"]), issue)
        self.assertEqual(self.store.run(run_id), history)
        self.assertEqual(history["wtr_project_id"], "wtr-project-1")
        self.assertEqual(template.read_text(), "Keep {{issue_id}}")
        self.assertEqual(list(prompt.directory().rglob("*.md")), [template])

    def test_ui_can_continue_editing_after_target_changes(self):
        with (
            patch.object(
                config_ui, "menu", side_effect=["target", "label", LABEL, True, "baseline", None]
            ),
            patch.object(config_ui, "input_text", return_value="HEAD"),
        ):
            config_ui.binding_settings(self.store, ("ws-uuid", "wtr-project-1"))
        self.assertEqual(
            self.store.project("ws-uuid", "wtr-project-1"), dict(self.label_row(), baseline="HEAD")
        )

    def test_ui_cancellation_at_each_step_preserves_project(self):
        for answers in ([None], ["label", None], ["label", LABEL, None], ["label", LABEL, False]):
            with (
                self.subTest(answers=answers),
                patch.object(config_ui, "menu", side_effect=answers),
            ):
                self.assertIsNone(config_ui.change_binding_target(self.store, self.binding))
            self.assertEqual(self.store.projects(), [self.binding])

    def test_type_cancel_does_not_query_linear(self):
        with (
            patch.object(config_ui, "menu", return_value=None),
            patch.object(config_ui, "Linear") as linear,
        ):
            config_ui.change_binding_target(self.store, self.binding)
        linear.assert_not_called()

    def test_ui_workspace_mismatch_keeps_project(self):
        with (
            patch.object(config_ui, "menu", return_value="label"),
            patch.object(config_ui, "Linear", return_value=SimpleNamespace(workspace="other")),
            self.assertRaisesRegex(Error, "workspace"),
        ):
            config_ui.change_binding_target(self.store, self.binding)
        self.assertEqual(self.store.projects(), [self.binding])

    def test_cli_aliases_change_matching_without_path_prompts(self):
        for command in ("project", "binding"):
            with self.subTest(command=command):
                result = self.cli(command, "edit", "wtr-project-1", "--label", "repo:app")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout), self.label_row())
                self.assertEqual(self.store.projects(), [self.label_row()])
                self.store.replace_binding(self.label_row(), self.binding)
        self.assertEqual(self.store.all("SELECT * FROM issues"), [])
        self.assertEqual(self.store.all("SELECT * FROM runs"), [])

    def test_cli_updates_target_and_explicit_path_settings_together(self):
        result = self.cli(
            "project", "edit", "wtr-project-1", "--label", "label-1", "--baseline", "HEAD"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        row = self.store.project("ws-uuid", "wtr-project-1")
        self.assertEqual(
            row,
            dict(
                self.label_row(),
                baseline="HEAD",
                repo=str(self.repo.resolve()),
                root=str((self.base / "trees").resolve()),
            ),
        )

    def test_cli_rejects_two_target_types_and_unknown_target(self):
        result = self.cli(
            "project", "edit", "wtr-project-1", "--project", "Project", "--label", "repo:app"
        )
        self.assertEqual(result.returncode, 2)
        result = self.cli("project", "edit", "wtr-project-1", "--label", "unknown")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.store.projects(), [self.binding])

    def test_conflicting_target_is_not_overwritten(self):
        target = self.label_row(project_id="wtr-project-2", name="Other app", baseline="HEAD")
        self.store.save_binding(target)
        result = self.cli("project", "edit", "wtr-project-1", "--label", "repo:app")
        self.assertEqual(result.returncode, 1)
        self.assertIn("已被其他绑定占用", result.stderr)
        self.assertEqual(self.store.project("ws-uuid", "wtr-project-1"), self.binding)
        self.assertEqual(self.store.project("ws-uuid", "wtr-project-2"), target)

    def test_concurrent_change_is_not_overwritten(self):
        original = dict(self.binding)
        self.store.save_binding(dict(original, baseline="HEAD"))
        with self.assertRaisesRegex(Error, "其他进程修改"):
            self.store.replace_binding(original, self.label_row())
        self.assertEqual(self.store.project("ws-uuid", "wtr-project-1")["baseline"], "HEAD")
        self.assertIsNone(self.store.binding("ws-uuid", "label", "label-1"))

    def test_replacement_cannot_change_local_identity_or_workspace(self):
        for values in ({"project_id": "wtr-project-2"}, {"workspace": "other-ws"}):
            with self.subTest(values=values), self.assertRaisesRegex(Error, "身份|workspace"):
                self.store.replace_binding(self.binding, dict(self.label_row(), **values))
            self.assertEqual(self.store.projects(), [self.binding])

    def test_label_to_project_and_same_type_changes_keep_local_identity_and_name(self):
        self.store.replace_binding(self.binding, self.label_row())
        fixture = json.loads(self.fixture.read_text())
        fixture["projects"].append({"id": "project-2", "name": "Another project"})
        self.fixture.write_text(json.dumps(fixture))
        result = self.cli("project", "edit", "wtr-project-1", "--project", "project-2")
        self.assertEqual(result.returncode, 0, result.stderr)
        row = self.store.project("ws-uuid", "wtr-project-1")
        self.assertEqual(
            row, dict(self.binding, target_id="project-2", target_name="Another project")
        )
        self.assertEqual(len(self.store.projects()), 1)
        result = self.cli("project", "edit", "wtr-project-1", "--project", "project-1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.store.projects(), [self.binding])

    def test_local_rename_preserves_remote_name_and_template(self):
        template = prompt.initialize("ws-uuid", "wtr-project-1")
        template.write_text("Local {{issue_id}}")
        renamed = dict(self.binding, name="Renamed local app")
        self.store.replace_binding(self.binding, renamed)
        self.assertEqual(self.store.projects(), [renamed])
        self.assertEqual(renamed["target_name"], "Project")
        self.assertEqual(template.read_text(), "Local {{issue_id}}")

    def test_remote_name_refresh_preserves_local_name_and_identity(self):
        changed = bindings.from_target(
            SimpleNamespace(workspace="ws-uuid", slug="test-space"),
            "project",
            {"id": "project-1", "name": "Renamed remote project"},
            self.binding,
        )
        self.store.replace_binding(self.binding, changed)
        self.assertEqual(
            self.store.projects(), [dict(self.binding, target_name="Renamed remote project")]
        )

    def test_transaction_rolls_back_if_project_update_fails(self):
        label = self.label_row()
        self.store.replace_binding(self.binding, label)
        self.store.write("""CREATE TRIGGER fail_project_update AFTER UPDATE ON wtr_projects
            BEGIN SELECT RAISE(ABORT, 'test project failure'); END""")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "test project failure"):
            self.store.replace_binding(label, self.binding)
        self.assertEqual(self.store.projects(), [label])
        self.assertIsNone(self.store.binding("ws-uuid", "project", "project-1"))
        self.store.write("DROP TRIGGER fail_project_update")
        self.store.replace_binding(label, self.binding)
        self.assertEqual(self.store.projects(), [self.binding])

    def test_disabling_retains_settings_and_removes_only_matching(self):
        self.store.remove_binding(*bindings.key(self.binding))
        self.assertEqual(self.store.bindings(), [])
        self.assertEqual(self.store.projects(), [dict(self.binding, enabled=0)])
        self.assertEqual(
            self.store.project("ws-uuid", "wtr-project-1"), dict(self.binding, enabled=0)
        )
        self.assertIsNone(self.store.binding(*bindings.key(self.binding)))

    def test_readonly_rejects_binding_replacement(self):
        readonly = Store(self.store.path, readonly=True)
        try:
            with self.assertRaisesRegex(Error, "只读"):
                readonly.replace_binding(self.binding, self.label_row())
        finally:
            readonly.close()
        self.assertEqual(self.store.projects(), [self.binding])
