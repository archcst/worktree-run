import contextlib
import io
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

from support import ISSUE, Sandbox

from worktree_run import bindings, prompt, worktree
from worktree_run.cli import resolve_binding
from worktree_run.linear import Linear
from worktree_run.store import Store
from worktree_run.util import Error

LABEL = {"id": "label-1", "name": "repo:app", "team": None}


def connection(nodes, *, more=False, cursor=None):
    return {"nodes": nodes, "pageInfo": {"hasNextPage": more, "endCursor": cursor}}


class BindingTests(Sandbox):
    def context(self):
        return SimpleNamespace(workspace="ws-uuid", slug="test-space")

    def labeled_issue(self, *labels, project=None):
        return dict(ISSUE, project=project, labels=connection(list(labels) or [LABEL]))

    def label_binding(self, **values):
        row = dict(self.binding, kind="label", target_id="label-1", name="repo:app")
        row.update(values)
        self.store.save_binding(row)
        return row

    def fixture_issue(self, issue):
        fixture = json.loads(self.fixture.read_text())
        fixture["issues"] = [issue]
        self.fixture.write_text(json.dumps(fixture))

    def test_project_and_label_bindings_match_by_id(self):
        label = self.label_binding()
        self.assertEqual(resolve_binding(self.store, self.context(), ISSUE, True), self.binding)
        renamed = dict(LABEL, name="任意完整标签")
        issue = self.labeled_issue(renamed)
        self.assertEqual(resolve_binding(self.store, self.context(), issue, True), label)
        # Similar names, even exact duplicate names in another team, cannot match this ID.
        with self.assertRaisesRegex(Error, "未匹配"):
            resolve_binding(
                self.store, self.context(), self.labeled_issue(dict(LABEL, id="other")), True
            )

    def test_workspace_isolation(self):
        self.label_binding(workspace="other", workspace_slug="other-space")
        with self.assertRaisesRegex(Error, "未匹配"):
            resolve_binding(self.store, self.context(), self.labeled_issue(), True)

    def test_multiple_label_matches_require_explicit_choice(self):
        first = self.label_binding()
        second_label = dict(LABEL, id="label-2", name="another-label")
        second = self.label_binding(target_id="label-2", name="another-label")
        issue = self.labeled_issue(LABEL, second_label)
        for dry_run in (True, False):
            with (
                patch("sys.stdin.isatty", return_value=False),
                self.assertRaisesRegex(Error, "多个"),
            ):
                resolve_binding(self.store, self.context(), issue, dry_run)
        with (
            patch("sys.stdin.isatty", return_value=True),
            patch("worktree_run.cli.choose", return_value=second) as choose,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(resolve_binding(self.store, self.context(), issue, False), second)
        self.assertCountEqual(choose.call_args.args[0], [first, second])
        self.assertEqual(self.store.all("SELECT * FROM issues"), [])

    def test_project_and_label_matches_have_no_implicit_priority(self):
        self.label_binding()
        issue = self.labeled_issue(project=ISSUE["project"])
        with self.assertRaisesRegex(Error, "多个"):
            resolve_binding(self.store, self.context(), issue, True)

    def test_existing_issue_keeps_repository_after_label_change_and_binding_removal(self):
        row = worktree.route(self.store, self.context(), ISSUE, self.binding)
        self.store.save_issue(row)
        self.store.write("DELETE FROM bindings")
        self.label_binding(repo="/another/repository")
        self.assertEqual(
            resolve_binding(self.store, self.context(), self.labeled_issue(), True), row
        )

    def test_missing_binding_is_actionable_and_dry_run_never_prompts_or_writes(self):
        before = self.store.path.read_bytes()
        with (
            patch("sys.stdin.isatty", return_value=True),
            patch("worktree_run.cli.choose") as choose,
            self.assertRaisesRegex(Error, "repo:app.*wtr config"),
        ):
            resolve_binding(self.store, self.context(), self.labeled_issue(), True)
        choose.assert_not_called()
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_interactive_missing_binding_registers_selected_full_label(self):
        values = {key: self.binding[key] for key in ("repo", "baseline", "root")}
        with (
            patch("sys.stdin.isatty", return_value=True),
            patch("worktree_run.cli.choose", side_effect=lambda rows, label: rows[0]),
            patch("worktree_run.cli.binding_values", return_value=values),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            row = resolve_binding(self.store, self.context(), self.labeled_issue(), False)
        self.assertEqual(row["kind"], "label")
        self.assertEqual(row["target_id"], LABEL["id"])
        self.assertEqual(self.store.binding(*bindings.key(row)), row)
        self.assertEqual(self.store.all("SELECT * FROM runs"), [])

    def test_interactive_guidance_sanitizes_remote_names(self):
        issue = self.labeled_issue(dict(LABEL, name="repo:\x1b[2J\x1b[Happ"))
        stdout = io.StringIO()
        with (
            patch("sys.stdin.isatty", return_value=True),
            patch("worktree_run.cli.choose", side_effect=KeyboardInterrupt),
            contextlib.redirect_stdout(stdout),
            self.assertRaises(KeyboardInterrupt),
        ):
            resolve_binding(self.store, self.context(), issue, False)
        self.assertNotIn("\x1b", stdout.getvalue())
        self.assertEqual(len(self.store.bindings()), 1)

    def test_initialization_preserves_history_without_importing_project_bindings(self):
        row = worktree.route(self.store, self.context(), ISSUE, self.binding)
        self.store.save_issue(row)
        run_id = self.store.new_run(row, "pi", ["pi", "prompt"], None)
        self.store.update_run(run_id, status="failed")
        path = self.base / "existing.db"
        with sqlite3.connect(path) as db:
            self.store.db.backup(db)
            db.executescript("""
                DROP TABLE bindings;
                DROP TABLE prompt_projects;
                CREATE TABLE projects (workspace,project_id,name,workspace_slug,repo,baseline,root);
                PRAGMA user_version=1;
            """)
            db.execute(
                "INSERT INTO projects VALUES (?,?,?,?,?,?,?)",
                (
                    "ws-uuid",
                    "project-1",
                    "Project",
                    "test-space",
                    str(self.repo),
                    "main",
                    "trees",
                ),
            )
        before = path.read_bytes()
        with self.assertRaisesRegex(Error, "wtr config"):
            Store(path, readonly=True)
        self.assertEqual(path.read_bytes(), before)
        reopened = Store(path)
        try:
            self.assertEqual(reopened.bindings(), [])
            self.assertEqual(reopened.projects(), [])
            self.assertEqual(len(reopened.all("SELECT * FROM projects")), 1)
            self.assertEqual(reopened.issue("ws-uuid", ISSUE["id"]), row)
            self.assertEqual(reopened.run(run_id), self.store.run(run_id))
            self.assertEqual(reopened.one("PRAGMA user_version")["user_version"], 2)
        finally:
            reopened.close()
        self.assertFalse(prompt.directory().exists())

    def test_label_binding_dry_run_preserves_project_prompt_scope(self):
        self.store.write("DELETE FROM bindings")
        self.label_binding()
        self.install_agent()
        self.store.set_setting("agent", "pi")
        template = prompt.initialize("ws-uuid", "project-1")
        template.write_text("PROJECT {{issue_id}}")
        self.fixture_issue(self.labeled_issue(project=ISSUE["project"]))
        before = self.store.path.read_bytes()
        result = self.cli("ENG-123", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        plan = json.loads(result.stdout)
        self.assertEqual(plan["issue"]["repo"], str(self.repo.resolve()))
        self.assertTrue(plan["argv"][-1].endswith("PROJECT ENG-123"))
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertFalse((self.base / "trees").exists())
        self.assertFalse((self.base / "tmux.sock").exists())

    def test_label_binding_without_project_uses_default_prompt(self):
        self.label_binding()
        self.install_agent()
        self.fixture_issue(self.labeled_issue())
        result = self.cli("ENG-123", "--agent", "pi", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        plan = json.loads(result.stdout)
        self.assertIsNone(plan["issue"]["project_id"])
        self.assertTrue(
            plan["argv"][-1].endswith(prompt.DEFAULT.replace("{{issue_id}}", "ENG-123"))
        )
        self.assertFalse(prompt.directory().exists())

    def test_cli_add_full_label_and_mutually_exclusive_types(self):
        result = self.cli(
            "binding",
            "add",
            "--label",
            "repo:app",
            "--baseline",
            "main",
            "--root",
            str(self.base / "trees"),
            cwd=self.repo,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        row = json.loads(result.stdout)
        self.assertEqual(row["kind"], "label")
        self.assertEqual(row["target_id"], "label-1")
        self.assertEqual(row["name"], "repo:app")
        result = self.cli(
            "binding", "add", "--label", "repo:app", "--project", "Project", cwd=self.repo
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(len(self.store.bindings()), 2)

    def test_cli_label_name_must_be_complete(self):
        result = self.cli(
            "binding",
            "add",
            "--label",
            "app",
            "--baseline",
            "main",
            "--root",
            str(self.base / "trees"),
            cwd=self.repo,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIsNone(self.store.binding("ws-uuid", "label", "label-1"))

    def test_same_name_labels_require_id_and_show_team(self):
        fixture = json.loads(self.fixture.read_text())
        fixture["issueLabels"].append(
            dict(
                LABEL,
                id="team-label",
                isGroup=False,
                team={"id": "team-1", "name": "Engineering", "key": "ENG"},
            )
        )
        self.fixture.write_text(json.dumps(fixture))
        result = self.cli(
            "binding",
            "add",
            "--label",
            "repo:app",
            "--baseline",
            "main",
            "--root",
            str(self.base / "trees"),
            cwd=self.repo,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("Engineering (ENG)", result.stderr)
        self.assertIn("Workspace", result.stderr)
        result = self.cli(
            "binding",
            "add",
            "--label",
            "team-label",
            "--baseline",
            "main",
            "--root",
            str(self.base / "trees"),
            cwd=self.repo,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["target_id"], "team-label")

    def test_linear_labels_paginate_and_exclude_groups(self):
        fixture = json.loads(self.fixture.read_text())
        fixture["issueLabels"] += [
            dict(LABEL, id="group", isGroup=True),
            dict(LABEL, id="last", isGroup=False),
        ]
        self.fixture.write_text(json.dumps(fixture))
        self.assertEqual([row["id"] for row in Linear().labels()], ["label-1", "last"])

    def test_issue_labels_paginate_before_matching(self):
        self.label_binding()
        linear = Linear()
        issue = self.labeled_issue(dict(LABEL, id="unbound"))
        issue["labels"] = connection(issue["labels"]["nodes"], more=True, cursor="next")
        with patch.object(
            linear, "query", return_value={"issue": {"labels": connection([LABEL])}}
        ) as query:
            hydrated = linear.validate(issue)
        self.assertEqual(query.call_args.args[1], {"id": ISSUE["id"], "after": "next"})
        self.assertEqual(len(hydrated["labels"]["nodes"]), 2)
        self.assertEqual(
            resolve_binding(self.store, linear, hydrated, True)["target_id"], "label-1"
        )

    def test_issue_label_errors_are_not_treated_as_no_labels(self):
        linear = Linear()
        for labels in (
            None,
            {},
            {"nodes": []},
            connection([{}]),
            connection([], more=True, cursor=None),
        ):
            with self.subTest(labels=labels), self.assertRaises(Error):
                linear.validate(dict(ISSUE, labels=labels))
        repeated = connection([], more=True, cursor="same")
        with patch.object(linear, "query", return_value={"issue": {"labels": repeated}}):
            with self.assertRaisesRegex(Error, "重复"):
                linear.validate(dict(ISSUE, labels=repeated))
