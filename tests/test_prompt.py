import argparse
import json
import os
import shlex
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from support import ISSUE, Sandbox

from worktree_run import prompt, runner, worktree
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
        row = dict(self.binding, project_id="project-2", name="Second")
        row.update(values)
        self.store.save_project(row)
        return row

    def test_templates_are_isolated_by_workspace_and_project_id(self):
        first = prompt.initialize("ws-uuid", "project-1")
        second = prompt.initialize("ws-uuid", "project-2")
        other_workspace = prompt.initialize("other-ws", "project-1")
        self.assertEqual(len({first, second, other_workspace}), 3)
        first.write_text("FIRST {{issue_id}}")
        second.write_text("SECOND {{issue_id}}")
        other_workspace.write_text("OTHER {{issue_id}}")
        for workspace, project, marker in (
            ("ws-uuid", "project-1", "FIRST"),
            ("ws-uuid", "project-2", "SECOND"),
            ("other-ws", "project-1", "OTHER"),
        ):
            rendered = prompt.render("ENG-123", "test-space", workspace, project)
            self.assertTrue(rendered.endswith(marker + " ENG-123"))
        self.assertEqual(
            prompt.initialize("ws-uuid", "project-1").read_text(), "FIRST {{issue_id}}"
        )

    def test_render_prepends_english_workspace_context(self):
        path = prompt.initialize("ws-uuid", "project-1")
        path.write_text("项目规则 {{issue_id}}")
        self.assertEqual(
            prompt.render("ENG-123", "test-space", "ws-uuid", "project-1"),
            "Linear workspace: test-space (organization ID: ws-uuid)\n"
            "All Linear queries must explicitly use --workspace test-space.\n\n"
            "项目规则 ENG-123",
        )

    def test_template_identity_cannot_escape_config_directory(self):
        for workspace, project in (
            ("../ws", "project-1"),
            ("ws", "../../project"),
            ("ws", ""),
            (".", "p"),
        ):
            with self.subTest(workspace=workspace, project=project), self.assertRaises(Error):
                prompt.prompt_path(workspace, project)

    def test_edit_explicit_project_only_changes_its_template(self):
        self.second_project()
        result = self.edit("Project")
        self.assertEqual(result.returncode, 0, result.stderr)
        path = prompt.prompt_path("ws-uuid", "project-1")
        self.assertEqual(path.read_text(), "项目规则 {{issue_id}}")
        self.assertEqual(Path(self.editor_log.read_text()), path)
        self.assertFalse(prompt.prompt_path("ws-uuid", "project-2").exists())
        self.assertIn(str(path), result.stdout)

    def test_edit_infers_project_from_repo_subdirectory(self):
        cwd = self.repo / "src"
        cwd.mkdir()
        result = self.edit(cwd=cwd)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            Path(self.editor_log.read_text()), prompt.prompt_path("ws-uuid", "project-1")
        )

    def test_edit_infers_project_from_associated_worktree(self):
        second = self.second_project()
        issue = dict(ISSUE, project={"id": second["project_id"], "name": second["name"]})
        context = SimpleNamespace(workspace="ws-uuid", slug="test-space")
        row = worktree.route(self.store, context, issue, second)
        self.store.save_issue(row)
        worktree.prepare(row, associated=False)
        cwd = Path(row["worktree"]) / "src"
        cwd.mkdir()
        result = self.edit(cwd=cwd)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            Path(self.editor_log.read_text()), prompt.prompt_path("ws-uuid", "project-2")
        )

    def test_shared_repo_requires_project_selection(self):
        self.second_project()
        result = self.edit(cwd=self.repo)
        self.assertEqual(result.returncode, 1)
        self.assertIn("显式指定", result.stderr)
        self.assertFalse(self.editor_log.exists())
        self.assertFalse(prompt.prompt_path("ws-uuid", "project-1").exists())
        result = self.edit("project-2", cwd=self.repo)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_outside_repo_selects_from_bound_projects(self):
        second = self.second_project()
        args = argparse.Namespace(project=None, workspace=None)
        with (
            patch("worktree_run.cli.Path.cwd", return_value=self.base),
            patch("worktree_run.cli.choose", return_value=second) as choose,
        ):
            self.assertEqual(prompt_project(self.store, args), second)
        self.assertEqual(len(choose.call_args.args[0]), 2)

    def test_workspace_disambiguates_same_project_names(self):
        self.second_project(workspace="other-ws", workspace_slug="other-space", name="Project")
        result = self.edit("Project")
        self.assertEqual(result.returncode, 1)
        self.assertFalse(self.editor_log.exists())
        result = self.edit("Project", "--workspace", "other-space")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            Path(self.editor_log.read_text()), prompt.prompt_path("other-ws", "project-2")
        )

    def test_prompt_edit_works_without_linear_network(self):
        fixture = json.loads(self.fixture.read_text())
        fixture["fail"] = True
        self.fixture.write_text(json.dumps(fixture))
        result = self.edit("project-1")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_invalid_template_and_editor_failure_are_reported(self):
        result = self.edit("project-1", text="missing placeholder")
        self.assertEqual(result.returncode, 1)
        self.assertIn("{{issue_id}}", result.stderr)
        with self.assertRaises(Error):
            prompt.render("ENG-123", "test-space", "ws-uuid", "project-1")
        result = self.edit("project-1", exit_code=2)
        self.assertEqual(result.returncode, 1)
        self.assertIn("编辑器退出失败", result.stderr)

    def test_no_project_uses_builtin_template_without_creating_files(self):
        self.assertIsNone(prompt.initialize("ws-uuid", None))
        rendered = prompt.render("ENG-123", "test-space", "ws-uuid", None)
        self.assertTrue(rendered.endswith(prompt.DEFAULT.replace("{{issue_id}}", "ENG-123")))
        self.assertFalse((self.base / "config").exists())

    def test_existing_unscoped_file_is_preserved(self):
        existing = self.base / "config/worktree-run/prompt.md"
        existing.parent.mkdir(parents=True)
        existing.write_text("Saved custom instructions {{issue_id}}")
        path = prompt.initialize("ws-uuid", "project-1")
        self.assertEqual(path.read_text(), prompt.DEFAULT)
        self.assertEqual(existing.read_text(), "Saved custom instructions {{issue_id}}")
        self.assertNotIn(
            "Saved custom", prompt.render("ENG-123", "test-space", "ws-uuid", "project-1")
        )

    def test_remove_and_rebind_preserves_project_template(self):
        path = prompt.initialize("ws-uuid", "project-1")
        path.write_text("Keep {{issue_id}}")
        self.assertEqual(self.cli("project", "remove", "project-1").returncode, 0)
        result = self.cli(
            "project",
            "add",
            "project-1",
            "--baseline",
            "main",
            "--root",
            str(self.base / "trees"),
            cwd=self.repo,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(path.read_text(), "Keep {{issue_id}}")

    def test_routing_and_saved_runs_use_project_template(self):
        self.install_agent()
        second = self.second_project()
        first_path = prompt.initialize("ws-uuid", "project-1")
        first_path.write_text("FIRST {{issue_id}}")
        prompt.initialize("ws-uuid", "project-2").write_text("SECOND {{issue_id}}")
        context = SimpleNamespace(workspace="ws-uuid", slug="test-space")
        with self.store.lock():
            run = runner.launch(self.store, context, ISSUE, self.binding, "pi")
        first_path.write_text("EDITED {{issue_id}}")
        self.wait_for(lambda: self.store.run(run["id"])["status"] == "awaiting_review")
        self.assertTrue(json.loads(self.store.run(run["id"])["argv"])[-1].endswith("FIRST ENG-123"))
        # A project change uses the current Linear project while retaining the worktree.
        moved = dict(ISSUE, project={"id": second["project_id"], "name": second["name"]})
        row, action, argv, _ = runner.plan(self.store, context, moved, second, "pi")
        self.assertEqual(action, "reuse")
        self.assertEqual(row["worktree"], run["cwd"])
        self.assertTrue(argv[-1].endswith("SECOND ENG-123"))
