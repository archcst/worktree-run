import argparse
import json
import os
import shlex
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from support import ISSUE, Sandbox

from worktree_run import bindings, prompt, runner, worktree
from worktree_run.cli import prompt_project
from worktree_run.util import Error


class ProjectPromptTests(Sandbox):
    def edit(self, *args, cwd=None, text="项目规则 {{issue_id}}", exit_code=0):
        editor = self.base / "test editor.py"
        self.editor_log = self.base / "edited-path.txt"
        editor.write_text(
            "import sys\nfrom pathlib import Path\n"
            f"Path({str(self.editor_log)!r}).write_text(sys.argv[1])\n"
            f"Path(sys.argv[1]).write_text({text!r}, encoding='utf-8')\n"
            f"raise SystemExit({exit_code})\n"
        )
        with patch.dict(
            os.environ, {"VISUAL": "", "EDITOR": shlex.join([sys.executable, str(editor)])}
        ):
            return self.cli("prompt", "edit", *args, cwd=cwd)

    def second_project(self, **values):
        row = dict(
            self.binding,
            project_id="wtr-project-2",
            target_id="project-2",
            target_name="Second remote",
            name="Second",
        )
        row.update(values)
        return self.store.save_binding(row)

    def test_templates_are_isolated_by_workspace_and_local_project_id(self):
        first = prompt.initialize("ws-uuid", "wtr-project-1")
        second = prompt.initialize("ws-uuid", "wtr-project-2")
        other_workspace = prompt.initialize("other-ws", "wtr-project-1")
        self.assertEqual(first, prompt.directory() / "ws-uuid/wtr/wtr-project-1.md")
        self.assertEqual(len({first, second, other_workspace}), 3)
        first.write_text("FIRST {{issue_id}}")
        second.write_text("SECOND {{issue_id}}")
        other_workspace.write_text("OTHER {{issue_id}}")
        for workspace, project, marker in (
            ("ws-uuid", "wtr-project-1", "FIRST"),
            ("ws-uuid", "wtr-project-2", "SECOND"),
            ("other-ws", "wtr-project-1", "OTHER"),
        ):
            rendered = prompt.render("ENG-123", "test-space", workspace, project)
            self.assertTrue(rendered.endswith(marker + " ENG-123"))
        self.assertEqual(
            prompt.initialize("ws-uuid", "wtr-project-1").read_text(), "FIRST {{issue_id}}"
        )

    def test_render_prepends_english_workspace_context(self):
        path = prompt.initialize("ws-uuid", "wtr-project-1")
        path.write_text("项目规则 {{issue_id}}")
        self.assertEqual(
            prompt.render("ENG-123", "test-space", "ws-uuid", "wtr-project-1"),
            "Linear workspace: test-space (organization ID: ws-uuid)\n"
            "All Linear queries must explicitly use --workspace test-space.\n\n"
            "项目规则 ENG-123",
        )

    def test_template_identity_cannot_escape_config_directory(self):
        for workspace, project in (
            ("../ws", "wtr-project-1"),
            ("ws", "../../project"),
            ("ws", ""),
            (".", "p"),
        ):
            with self.subTest(workspace=workspace, project=project), self.assertRaises(Error):
                prompt.prompt_path(workspace, project)

    def test_edit_explicit_local_project_only_changes_its_template(self):
        self.second_project()
        result = self.edit("Project")
        self.assertEqual(result.returncode, 0, result.stderr)
        path = prompt.prompt_path("ws-uuid", "wtr-project-1")
        self.assertEqual(path.read_text(), "项目规则 {{issue_id}}")
        self.assertEqual(Path(self.editor_log.read_text()), path)
        self.assertFalse(prompt.prompt_path("ws-uuid", "wtr-project-2").exists())
        self.assertIn(str(path), result.stdout)

    def test_edit_infers_project_from_repo_subdirectory(self):
        cwd = self.repo / "src"
        cwd.mkdir()
        result = self.edit(cwd=cwd)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            Path(self.editor_log.read_text()), prompt.prompt_path("ws-uuid", "wtr-project-1")
        )

    def test_edit_infers_pinned_local_project_from_associated_worktree(self):
        second = self.second_project(kind="label", target_id="label-1", target_name="repo:app")
        context = SimpleNamespace(workspace="ws-uuid", slug="test-space")
        row = worktree.route(self.store, context, dict(ISSUE, project=None), second)
        self.store.save_issue(row)
        worktree.prepare(row, associated=False)
        self.store.remove_binding(*bindings.key(second))
        cwd = Path(row["worktree"]) / "src"
        cwd.mkdir()
        result = self.edit(cwd=cwd)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            Path(self.editor_log.read_text()), prompt.prompt_path("ws-uuid", "wtr-project-2")
        )

    def test_shared_repo_requires_project_selection(self):
        self.second_project()
        result = self.edit(cwd=self.repo)
        self.assertEqual(result.returncode, 1)
        self.assertIn("显式指定", result.stderr)
        self.assertFalse(self.editor_log.exists())
        self.assertFalse(prompt.prompt_path("ws-uuid", "wtr-project-1").exists())
        result = self.edit("wtr-project-2", cwd=self.repo)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_outside_repo_selects_from_local_projects_without_linear(self):
        second = self.second_project()
        args = argparse.Namespace(project=None, workspace=None)
        with (
            patch("worktree_run.cli.Path.cwd", return_value=self.base),
            patch("worktree_run.cli.choose", return_value=second) as choose,
            patch("worktree_run.cli.Linear") as linear,
        ):
            self.assertEqual(prompt_project(self.store, args), second)
        self.assertCountEqual(choose.call_args.args[0], [self.binding, second])
        linear.assert_not_called()

    def test_workspace_disambiguates_same_local_project_names(self):
        self.second_project(workspace="other-ws", workspace_slug="other-space", name="Project")
        result = self.edit("Project")
        self.assertEqual(result.returncode, 1)
        self.assertFalse(self.editor_log.exists())
        result = self.edit("Project", "--workspace", "other-space")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            Path(self.editor_log.read_text()), prompt.prompt_path("other-ws", "wtr-project-2")
        )

    def test_prompt_edit_uses_local_name_without_linear_network(self):
        self.store.save_binding(dict(self.binding, name="Local app"))
        fixture = json.loads(self.fixture.read_text())
        fixture["fail"] = True
        self.fixture.write_text(json.dumps(fixture))
        result = self.edit("Local app")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Local app", result.stdout)
        self.assertEqual(
            Path(self.editor_log.read_text()), prompt.prompt_path("ws-uuid", "wtr-project-1")
        )

    def test_invalid_template_and_editor_failure_are_reported(self):
        result = self.edit("wtr-project-1", text="missing placeholder")
        self.assertEqual(result.returncode, 1)
        self.assertIn("{{issue_id}}", result.stderr)
        with self.assertRaises(Error):
            prompt.render("ENG-123", "test-space", "ws-uuid", "wtr-project-1")
        result = self.edit("wtr-project-1", exit_code=2)
        self.assertEqual(result.returncode, 1)
        self.assertIn("编辑器退出失败", result.stderr)

    def test_missing_local_template_uses_builtin_without_creating_files(self):
        rendered = prompt.render("ENG-123", "test-space", "ws-uuid", "wtr-project-1")
        self.assertTrue(rendered.endswith(prompt.DEFAULT.replace("{{issue_id}}", "ENG-123")))
        self.assertFalse((self.base / "config").exists())

    def test_existing_unscoped_file_is_preserved(self):
        existing = self.base / "config/worktree-run/prompt.md"
        existing.parent.mkdir(parents=True)
        existing.write_text("Saved custom instructions {{issue_id}}")
        path = prompt.initialize("ws-uuid", "wtr-project-1")
        self.assertEqual(path.read_text(), prompt.DEFAULT)
        self.assertEqual(existing.read_text(), "Saved custom instructions {{issue_id}}")
        self.assertNotIn(
            "Saved custom", prompt.render("ENG-123", "test-space", "ws-uuid", "wtr-project-1")
        )

    def test_disabled_project_retains_editable_template(self):
        path = prompt.initialize("ws-uuid", "wtr-project-1")
        path.write_text("Keep {{issue_id}}")
        result = self.cli("project", "remove", "wtr-project-1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.store.project("ws-uuid", "wtr-project-1")["enabled"], 0)
        self.assertEqual(path.read_text(), "Keep {{issue_id}}")
        result = self.edit("wtr-project-1", text="Disabled {{issue_id}}")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(path.read_text(), "Disabled {{issue_id}}")

    def test_remote_project_changes_keep_local_template_and_saved_run_snapshot(self):
        self.install_agent()
        second = self.second_project()
        first_path = prompt.initialize("ws-uuid", "wtr-project-1")
        first_path.write_text("FIRST {{issue_id}}")
        prompt.initialize("ws-uuid", "wtr-project-2").write_text("SECOND {{issue_id}}")
        context = SimpleNamespace(workspace="ws-uuid", slug="test-space")
        with self.store.lock():
            run = runner.launch(self.store, context, ISSUE, self.binding, "pi")
        first_path.write_text("EDITED {{issue_id}}")
        self.wait_for(lambda: self.store.run(run["id"])["status"] == "awaiting_review")
        saved = self.store.run(run["id"])
        self.assertEqual(saved["wtr_project_id"], "wtr-project-1")
        self.assertTrue(json.loads(saved["argv"])[-1].endswith("FIRST ENG-123"))
        self.store.remove_binding(*bindings.key(self.binding))
        for remote in ({"id": second["target_id"], "name": second["target_name"]}, None):
            with self.subTest(remote=remote):
                moved = dict(ISSUE, project=remote)
                row, action, argv, _ = runner.plan(self.store, context, moved, second, "pi")
                self.assertEqual(action, "reuse")
                self.assertEqual(row["worktree"], run["cwd"])
                self.assertEqual(row["wtr_project_id"], "wtr-project-1")
                self.assertEqual(row["project_id"], remote["id"] if remote else None)
                self.assertTrue(argv[-1].endswith("EDITED ENG-123"))
        self.assertEqual(self.store.run(run["id"]), saved)

    def test_matching_target_change_preserves_template_for_new_issues(self):
        self.install_agent()
        context = SimpleNamespace(workspace="ws-uuid", slug="test-space")
        path = prompt.initialize("ws-uuid", "wtr-project-1")
        path.write_text("LOCAL {{issue_id}}")
        label = {"id": "label-1", "name": "repo:app", "team": None}
        changed = bindings.from_target(context, "label", label, self.binding)
        self.store.replace_binding(self.binding, changed)
        issue = dict(ISSUE, project=None, labels={"nodes": [label]})
        row, _, argv, _ = runner.plan(self.store, context, issue, changed, "pi")
        self.assertIsNone(row["project_id"])
        self.assertEqual(row["wtr_project_id"], "wtr-project-1")
        self.assertTrue(argv[-1].endswith("LOCAL ENG-123"))
        self.assertEqual(list(prompt.directory().rglob("*.md")), [path])
