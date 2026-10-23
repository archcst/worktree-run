import sqlite3
import uuid
from types import SimpleNamespace
from unittest.mock import patch

from support import ISSUE, Sandbox

from worktree_run import bindings, migration, prompt, runner, worktree
from worktree_run.store import Store
from worktree_run.util import Error

LEGACY_SCHEMA = """
CREATE TABLE settings (key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE projects (workspace TEXT,project_id TEXT,name TEXT,workspace_slug TEXT,repo TEXT,baseline TEXT,root TEXT);
CREATE TABLE bindings (workspace TEXT,kind TEXT,target_id TEXT,name TEXT,workspace_slug TEXT,team_name TEXT,repo TEXT,baseline TEXT,root TEXT,PRIMARY KEY(workspace,kind,target_id));
CREATE TABLE prompt_projects (workspace TEXT,project_id TEXT,name TEXT,workspace_slug TEXT);
CREATE TABLE issues (workspace TEXT,issue_id TEXT,identifier TEXT,workspace_slug TEXT,project_id TEXT,repo TEXT,baseline TEXT,branch TEXT,worktree TEXT,PRIMARY KEY(workspace,issue_id));
CREATE TABLE runs (id TEXT PRIMARY KEY,workspace TEXT,issue_id TEXT,identifier TEXT,agent TEXT,argv TEXT,cwd TEXT,workspace_slug TEXT,status TEXT,created_at TEXT,started_at TEXT,ended_at TEXT,exit_code INTEGER,error TEXT,session_id TEXT,window_id TEXT,pane_id TEXT,tmux_socket TEXT,launch_command TEXT,agent_session TEXT);
PRAGMA user_version=2;
"""
CUSTOM = "仓库统一规则 {{issue_id}}\n完成浏览器验收后汇报。\n"


class MigrationTests(Sandbox):
    def legacy(self, *, with_old_project=True):
        path = self.base / "legacy.db"
        context = SimpleNamespace(workspace="ws-uuid", slug="test-space")
        for index, project in enumerate(
            ({"id": "old-project", "name": "app"}, ISSUE["project"], None)
        ):
            issue = dict(ISSUE, id=f"issue-{index}", identifier=f"ENG-{index + 1}", project=project)
            row = worktree.route(self.store, context, issue, self.binding)
            self.store.save_issue(row)
            run_id = self.store.new_run(row, "pi", ["pi", f"snapshot-{index}"], None)
            self.store.update_run(run_id, status="running" if index == 0 else "awaiting_review")
        with sqlite3.connect(path) as db:
            db.executescript(LEGACY_SCHEMA)
            for table in ("issues", "runs"):
                for row in self.store.all(f"SELECT * FROM {table}"):
                    row.pop("wtr_project_id")
                    db.execute(
                        f"INSERT INTO {table} ({','.join(row)}) VALUES ({','.join('?' for _ in row)})",
                        tuple(row.values()),
                    )
            db.execute("INSERT INTO settings VALUES ('agent','pi')")
            db.execute("INSERT INTO settings VALUES ('tmux-session','wtr')")
            db.execute(
                "INSERT INTO bindings VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    "ws-uuid",
                    "label",
                    "repo-label",
                    "repo:app",
                    "test-space",
                    "Team (ENG)",
                    str(self.repo),
                    "main",
                    str(self.base / "trees"),
                ),
            )
            if with_old_project:
                db.execute(
                    "INSERT INTO projects VALUES (?,?,?,?,?,?,?)",
                    (
                        "ws-uuid",
                        "old-project",
                        "app",
                        "test-space",
                        str(self.repo),
                        "main",
                        str(self.base / "trees"),
                    ),
                )
            for project_id in ("old-project", "project-1"):
                db.execute(
                    "INSERT INTO prompt_projects VALUES (?,?,?,?)",
                    ("ws-uuid", project_id, project_id, "test-space"),
                )
        original = migration.legacy_template("ws-uuid", "old-project")
        original.parent.mkdir(parents=True, exist_ok=True)
        original.write_text(CUSTOM)
        migration.legacy_template("ws-uuid", "project-1").write_text(prompt.DEFAULT)
        return path

    def snapshot(self, path):
        with sqlite3.connect(path) as db:
            db.row_factory = sqlite3.Row
            return {
                table: [dict(r) for r in db.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                for table in (
                    "issues",
                    "runs",
                    "settings",
                    "projects",
                    "bindings",
                    "prompt_projects",
                )
            }

    def test_upgrade_restores_repository_template_and_preserves_execution_snapshots(self):
        path = self.legacy()
        before = self.snapshot(path)
        originals = {p: p.read_bytes() for p in prompt.directory().rglob("*.md")}
        with (
            patch("worktree_run.linear.Linear", side_effect=AssertionError("network")),
            patch("worktree_run.runner.recover", side_effect=AssertionError("recover")),
            patch("worktree_run.runner.launch", side_effect=AssertionError("launch")),
        ):
            store = Store(path)
        try:
            (project,) = store.bindings()
            self.assertEqual(project["name"], "app")
            self.assertEqual(project["kind"], "label")
            self.assertEqual(project["target_id"], "repo-label")
            self.assertEqual(project["repo"], str(self.repo))
            template = prompt.prompt_path("ws-uuid", project["project_id"])
            self.assertEqual(template.read_text(), CUSTOM)
            self.assertTrue(store.migration_backup.exists())
            self.assertEqual(store.migration_backup.stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.snapshot(store.migration_backup), before)
            for table, rows in before.items():
                current = store.all(f"SELECT * FROM {table} ORDER BY rowid")
                for row in current:
                    if table in ("issues", "runs"):
                        self.assertEqual(row.pop("wtr_project_id"), project["project_id"])
                self.assertEqual(current, rows)
            self.install_agent()
            issue = dict(ISSUE, id="issue-2", identifier="ENG-3", project=None)
            _, _, argv, session = runner.plan(
                store, SimpleNamespace(workspace="ws-uuid", slug="test-space"), issue, project, "pi"
            )
            self.assertTrue(argv[-1].endswith(CUSTOM.replace("{{issue_id}}", "ENG-3")))
            self.assertEqual(session, "wtr")
            changed = bindings.from_target(
                SimpleNamespace(workspace="ws-uuid", slug="test-space"),
                "project",
                {"id": "another", "name": "Remote renamed"},
                dict(project),
            )
            store.replace_binding(project, changed)
            self.assertEqual(changed["project_id"], project["project_id"])
            self.assertEqual(changed["name"], "app")
            self.assertEqual(template.read_text(), CUSTOM)
        finally:
            store.close()
        for source, content in originals.items():
            self.assertEqual(source.read_bytes(), content)
        reopened = Store(path)
        self.assertIsNone(reopened.migration_backup)
        reopened.close()

    def test_readonly_open_never_migrates_or_creates_backup(self):
        path = self.legacy()
        before = path.read_bytes()
        with self.assertRaisesRegex(Error, "wtr config"):
            Store(path, readonly=True)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(self.base.glob("legacy.db.backup*")), [])
        self.assertFalse((prompt.directory() / "ws-uuid/wtr").exists())

    def test_cli_list_commands_do_not_upgrade_or_write_templates(self):
        path = self.legacy()
        before = path.read_bytes()
        for command in (
            ("config", "list"),
            ("project", "list"),
            ("binding", "list"),
            ("agent", "list"),
        ):
            with self.subTest(command=command):
                result = self.cli(*command, "--db", str(path))
                self.assertEqual(result.returncode, 1)
                self.assertIn("wtr config", result.stderr)
                self.assertEqual(path.read_bytes(), before)
                self.assertFalse((prompt.directory() / "ws-uuid/wtr").exists())
        self.assertEqual(list(self.base.glob("legacy.db.backup*")), [])

    def test_remote_project_does_not_disambiguate_historical_local_ownership(self):
        path = self.legacy()
        with sqlite3.connect(path) as db:
            db.execute(
                "INSERT INTO bindings VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    "ws-uuid",
                    "project",
                    "project-1",
                    "Other",
                    "test-space",
                    "",
                    str(self.repo),
                    "main",
                    str(self.base / "trees"),
                ),
            )
        before = self.snapshot(path)
        with self.assertRaisesRegex(Error, "对应多个 WTR 项目"):
            Store(path)
        self.assertEqual(self.snapshot(path), before)

    def test_default_legacy_template_does_not_hide_unique_custom_template(self):
        path = self.legacy()
        migration.legacy_template("ws-uuid", "old-project").write_text(prompt.DEFAULT)
        migration.legacy_template("ws-uuid", "project-1").write_text(CUSTOM)
        store = Store(path)
        try:
            (project,) = store.projects()
            self.assertEqual(
                prompt.prompt_path("ws-uuid", project["project_id"]).read_text(), CUSTOM
            )
        finally:
            store.close()

    def test_upgrade_from_original_project_binding(self):
        path = self.legacy()
        with sqlite3.connect(path) as db:
            db.execute("DROP TABLE bindings")
            db.execute("DROP TABLE prompt_projects")
            db.execute("PRAGMA user_version=1")
        store = Store(path)
        try:
            (project,) = store.bindings()
            self.assertEqual(project["target_id"], "old-project")
            self.assertEqual(
                prompt.prompt_path("ws-uuid", project["project_id"]).read_text(), CUSTOM
            )
        finally:
            store.close()

    def test_conflicting_custom_templates_abort_without_data_loss(self):
        path = self.legacy()
        migration.legacy_template("ws-uuid", "project-1").write_text("Other {{issue_id}}")
        before = self.snapshot(path)
        with self.assertRaisesRegex(Error, "多份不同"):
            Store(path)
        self.assertEqual(self.snapshot(path), before)
        with sqlite3.connect(path) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertIsNone(
                db.execute("SELECT name FROM sqlite_master WHERE name='wtr_projects'").fetchone()
            )

    def test_failed_database_commit_can_retry_without_overwriting_template(self):
        path = self.legacy()
        with sqlite3.connect(path) as db:
            db.execute(
                "CREATE TRIGGER fail_update BEFORE UPDATE ON issues BEGIN SELECT RAISE(ABORT,'test failure'); END"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            Store(path)
        (template,) = (prompt.directory() / "ws-uuid/wtr").glob("*.md")
        self.assertEqual(template.read_text(), CUSTOM)
        before = template.stat().st_mtime_ns
        with sqlite3.connect(path) as db:
            db.execute("DROP TRIGGER fail_update")
        store = Store(path)
        try:
            self.assertEqual(template.stat().st_mtime_ns, before)
            self.assertEqual(len(store.projects()), 1)
        finally:
            store.close()

    def test_template_publication_failure_leaves_no_partial_destination(self):
        path = self.legacy()
        with patch("worktree_run.migration.os.link", side_effect=OSError("disk error")):
            with self.assertRaises(OSError):
                Store(path)
        self.assertEqual(list((prompt.directory() / "ws-uuid/wtr").iterdir()), [])
        store = Store(path)
        try:
            (project,) = store.projects()
            self.assertEqual(
                prompt.prompt_path("ws-uuid", project["project_id"]).read_text(), CUSTOM
            )
        finally:
            store.close()

    def test_existing_different_destination_is_not_overwritten(self):
        path = self.legacy()
        identity = str(uuid.uuid5(uuid.NAMESPACE_URL, "wtr:v3:ws-uuid:label:repo-label"))
        destination = prompt.initialize("ws-uuid", identity)
        destination.write_text("Already customized {{issue_id}}")
        with self.assertRaisesRegex(Error, "已有不同内容"):
            Store(path)
        self.assertEqual(destination.read_text(), "Already customized {{issue_id}}")

    def test_history_with_removed_rule_gets_disabled_local_project(self):
        path = self.legacy()
        with sqlite3.connect(path) as db:
            db.execute("DELETE FROM bindings")
        store = Store(path)
        try:
            self.assertEqual(store.bindings(), [])
            (project,) = store.projects()
            self.assertEqual(project["enabled"], 0)
            self.assertEqual(
                prompt.prompt_path("ws-uuid", project["project_id"]).read_text(), CUSTOM
            )
            self.assertTrue(
                all(
                    i["wtr_project_id"] == project["project_id"]
                    for i in store.all("SELECT * FROM issues")
                )
            )
        finally:
            store.close()
