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
    def label_row(self):
        return bindings.from_target(
            SimpleNamespace(workspace="ws-uuid", slug="test-space"),
            "label",
            LABEL,
            {field: self.binding[field] for field in ("repo", "baseline", "root")},
        )

    def test_ui_switch_preserves_paths_history_and_project_prompt(self):
        context = SimpleNamespace(workspace="ws-uuid", slug="test-space")
        issue = worktree.route(self.store, context, ISSUE, self.binding)
        self.store.save_issue(issue)
        run_id = self.store.new_run(issue, "pi", ["pi", "prompt"], None)
        self.store.update_run(run_id, status="failed")
        history = self.store.run(run_id)
        template = prompt.initialize("ws-uuid", "project-1")
        template.write_text("Keep {{issue_id}}")
        answers = iter(["label", LABEL, True])

        def answer(*args, **kwargs):
            with self.store.path.with_suffix(".lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(lock, fcntl.LOCK_UN)
            return next(answers)

        with patch.object(config_ui, "menu", side_effect=answer):
            key = config_ui.change_binding_target(self.store, self.binding)
        self.assertEqual(key, ("ws-uuid", "label", "label-1"))
        self.assertEqual(self.store.binding(*key), self.label_row())
        self.assertIsNone(self.store.binding("ws-uuid", "project", "project-1"))
        self.assertEqual(self.store.issue("ws-uuid", ISSUE["id"]), issue)
        self.assertEqual(self.store.run(run_id), history)
        self.assertEqual(template.read_text(), "Keep {{issue_id}}")
        self.assertIsNotNone(self.store.project("ws-uuid", "project-1"))

    def test_ui_can_continue_editing_after_key_changes(self):
        with (
            patch.object(
                config_ui, "menu", side_effect=["target", "label", LABEL, True, "baseline", None]
            ),
            patch.object(config_ui, "input_text", return_value="HEAD"),
        ):
            config_ui.binding_settings(self.store, bindings.key(self.binding))
        self.assertEqual(self.store.binding("ws-uuid", "label", "label-1")["baseline"], "HEAD")

    def test_ui_cancellation_at_each_step_preserves_binding(self):
        for answers in ([None], ["label", None], ["label", LABEL, None], ["label", LABEL, False]):
            with (
                self.subTest(answers=answers),
                patch.object(config_ui, "menu", side_effect=answers),
            ):
                self.assertIsNone(config_ui.change_binding_target(self.store, self.binding))
            self.assertEqual(self.store.bindings(), [self.binding])

    def test_type_cancel_does_not_query_linear(self):
        with (
            patch.object(config_ui, "menu", return_value=None),
            patch.object(config_ui, "Linear") as linear,
        ):
            config_ui.change_binding_target(self.store, self.binding)
        linear.assert_not_called()

    def test_ui_workspace_mismatch_keeps_binding(self):
        with (
            patch.object(config_ui, "menu", return_value="label"),
            patch.object(config_ui, "Linear", return_value=SimpleNamespace(workspace="other")),
            self.assertRaisesRegex(Error, "workspace"),
        ):
            config_ui.change_binding_target(self.store, self.binding)
        self.assertEqual(self.store.bindings(), [self.binding])

    def test_cli_converts_project_to_label_without_path_prompts(self):
        result = self.cli(
            "binding", "edit", "project-1", "--kind", "project", "--label", "repo:app"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), self.label_row())
        self.assertEqual(self.store.bindings(), [self.label_row()])
        self.assertEqual(self.store.all("SELECT * FROM issues"), [])
        self.assertEqual(self.store.all("SELECT * FROM runs"), [])

    def test_cli_updates_target_and_explicit_path_settings_together(self):
        result = self.cli(
            "binding", "edit", "project-1", "--label", "label-1", "--baseline", "HEAD"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        row = self.store.binding("ws-uuid", "label", "label-1")
        self.assertEqual(row["baseline"], "HEAD")
        self.assertEqual(row["repo"], str(self.repo.resolve()))
        self.assertEqual(row["root"], str((self.base / "trees").resolve()))

    def test_cli_rejects_two_target_types_and_unknown_target(self):
        result = self.cli(
            "binding", "edit", "project-1", "--project", "Project", "--label", "repo:app"
        )
        self.assertEqual(result.returncode, 2)
        result = self.cli("binding", "edit", "project-1", "--label", "unknown")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.store.bindings(), [self.binding])

    def test_conflicting_target_is_not_overwritten(self):
        target = dict(self.label_row(), baseline="HEAD")
        self.store.save_binding(target)
        result = self.cli("binding", "edit", "project-1", "--label", "repo:app")
        self.assertEqual(result.returncode, 1)
        self.assertIn("已被其他绑定占用", result.stderr)
        self.assertEqual(self.store.binding(*bindings.key(self.binding)), self.binding)
        self.assertEqual(self.store.binding(*bindings.key(target)), target)

    def test_concurrent_change_is_not_overwritten(self):
        original = dict(self.binding)
        self.store.save_binding(dict(original, baseline="HEAD"))
        with self.assertRaisesRegex(Error, "其他进程修改"):
            self.store.replace_binding(original, self.label_row())
        self.assertEqual(self.store.binding(*bindings.key(original))["baseline"], "HEAD")
        self.assertIsNone(self.store.binding("ws-uuid", "label", "label-1"))

    def test_label_to_project_caches_identity_and_same_type_can_change_target(self):
        self.store.replace_binding(self.binding, self.label_row())
        fixture = json.loads(self.fixture.read_text())
        fixture["projects"].append({"id": "project-2", "name": "Another project"})
        self.fixture.write_text(json.dumps(fixture))
        result = self.cli("binding", "edit", "label-1", "--project", "project-2")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.store.project("ws-uuid", "project-2")["name"], "Another project")
        result = self.cli("binding", "edit", "project-2", "--project", "project-1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.store.bindings(), [self.binding])

    def test_same_target_refreshes_display_name(self):
        renamed = dict(self.binding, name="Renamed project")
        self.store.replace_binding(self.binding, renamed)
        self.assertEqual(self.store.bindings(), [renamed])
        self.assertEqual(self.store.project("ws-uuid", "project-1")["name"], "Renamed project")

    def test_transaction_rolls_back_if_prompt_cache_write_fails(self):
        self.store.replace_binding(self.binding, self.label_row())
        self.store.write("""CREATE TRIGGER fail_project_cache BEFORE INSERT ON prompt_projects
            BEGIN SELECT RAISE(ABORT, 'test cache failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.replace_binding(self.label_row(), self.binding)
        self.assertEqual(self.store.bindings(), [self.label_row()])

    def test_readonly_rejects_binding_replacement(self):
        readonly = Store(self.store.path, readonly=True)
        try:
            with self.assertRaisesRegex(Error, "只读"):
                readonly.replace_binding(self.binding, self.label_row())
        finally:
            readonly.close()
        self.assertEqual(self.store.bindings(), [self.binding])
