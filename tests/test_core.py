import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from support import ISSUE, Sandbox

from worktree_run import agents, prompt, runner, worktree
from worktree_run.cli import main, select_issue
from worktree_run.linear import Linear
from worktree_run.store import Store
from worktree_run.util import Error


class CoreTests(Sandbox):
    def context(self):
        return SimpleNamespace(workspace="ws-uuid", slug="test-space")

    def row(self):
        return worktree.route(self.store, self.context(), ISSUE, self.binding)

    def test_prompt_unicode_and_parameter_boundaries(self):
        path = prompt.initialize("ws-uuid", "project-1")
        path.write_text("引号 '\" $(touch /tmp/not-executed)\n{{issue_id}}\n" + "长" * 30000)
        text = prompt.render("ENG-123", "test-space", "ws-uuid", "project-1")
        argv = agents.get("pi").argv(text)
        self.assertEqual(argv, ["pi", "--approve", argv[-1]])
        self.assertIn("ENG-123", argv[-1])
        self.assertIn("--workspace test-space", argv[-1])
        self.assertIn("\n", argv[-1])
        path.write_text("no placeholder")
        with self.assertRaises(Error):
            prompt.render("ENG-123", "test-space", "ws-uuid", "project-1")

    def test_create_reuse_and_dirty_main(self):
        (self.repo / "file.txt").write_text("uncommitted")
        row = self.row()
        self.store.save_issue(row)
        worktree.prepare(row, associated=False)
        self.assertEqual(worktree.inspect(row, associated=True), "reuse")
        worktree.prepare(row, associated=True)
        self.assertEqual((Path(row["worktree"]) / "file.txt").read_text(), "initial\n")
        self.assertEqual((self.repo / "file.txt").read_text(), "uncommitted")
        worktree.git(row["worktree"], "checkout", "--detach")
        with self.assertRaisesRegex(Error, "分支或路径"):
            worktree.inspect(row, associated=True)

    def test_path_and_branch_conflict(self):
        row = self.row()
        path = Path(row["worktree"])
        path.mkdir(parents=True)
        (path / "keep").write_text("keep")
        with self.assertRaisesRegex(Error, "占用"):
            worktree.prepare(row, associated=False)
        self.assertEqual((path / "keep").read_text(), "keep")
        row["worktree"] += "-other"
        worktree.git(self.repo, "branch", row["branch"])
        with self.assertRaisesRegex(Error, "分支已存在"):
            worktree.prepare(row, associated=False)
        worktree.git(self.repo, "worktree", "add", str(self.base / "external"), row["branch"])
        with self.assertRaisesRegex(Error, "其他 worktree"):
            worktree.prepare(row, associated=False)

    def test_bad_baseline_and_failed_remote_fetch(self):
        row = self.row()
        row["baseline"] = "missing"
        with self.assertRaisesRegex(Error, "基线不可解析"):
            worktree.prepare(row, associated=False)
        worktree.git(self.repo, "remote", "add", "origin", str(self.base / "missing-remote"))
        worktree.git(self.repo, "update-ref", "refs/remotes/origin/main", "HEAD")
        row["baseline"] = "origin/main"
        with self.assertRaises(Error):
            worktree.prepare(row, associated=False)
        self.assertFalse(Path(row["worktree"]).exists())
        self.assertNotEqual(
            worktree.git(
                self.repo, "show-ref", "--verify", "refs/heads/" + row["branch"], check=False
            ).returncode,
            0,
        )

    def test_fetch_uses_current_remote_branch(self):
        remote = self.base / "remote.git"
        worktree.git(self.repo, "clone", "--bare", str(self.repo), str(remote))
        worktree.git(self.repo, "remote", "add", "origin", str(remote))
        worktree.git(self.repo, "fetch", "origin")
        (self.repo / "file.txt").write_text("new remote content")
        worktree.git(self.repo, "commit", "-am", "new")
        worktree.git(self.repo, "push", "origin", "main")
        worktree.git(self.repo, "update-ref", "refs/remotes/origin/main", "HEAD~1")
        row = self.row()
        row["baseline"] = "origin/main"
        worktree.prepare(row, associated=False)
        self.assertEqual((Path(row["worktree"]) / "file.txt").read_text(), "new remote content")

    def test_cross_workspace_collision_and_existing_association(self):
        first = self.row()
        self.store.save_issue(first)
        worktree.prepare(first, associated=False)
        second = worktree.route(
            self.store, SimpleNamespace(workspace="other", slug="other"), ISSUE, self.binding
        )
        self.assertNotEqual(first["branch"], second["branch"])
        self.assertNotEqual(first["worktree"], second["worktree"])
        self.assertEqual(
            worktree.route(self.store, self.context(), ISSUE, {"repo": "invalid"}), first
        )
        worktree.prepare(second, associated=False)

    def test_unique_active_claim_and_multiple_batches(self):
        row = self.row()
        self.store.save_issue(row)
        first = self.store.new_run(row, "pi", ["pi", "text"], None)
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.new_run(row, "pi", ["pi", "text"], None)
        self.store.update_run(first, status="awaiting_review")
        self.assertNotEqual(first, self.store.new_run(row, "pi", ["pi", "text"], None))

    def test_recovery_does_not_overwrite_concurrent_exit(self):
        row = self.row()
        self.store.save_issue(row)
        run_id = self.store.new_run(row, "fake", ["git", "prompt"], None)
        self.store.update_run(run_id, status="running")

        def finish_during_inspection(*_):
            self.store.update_run(run_id, status="awaiting_review", exit_code=0)
            return {"dead": "1"}

        with (
            self.store.lock(),
            patch("worktree_run.tmux.Tmux.locate", side_effect=finish_during_inspection),
        ):
            runner.recover(self.store)
        self.assertEqual(self.store.run(run_id)["status"], "awaiting_review")

    def test_readonly_store_creates_nothing(self):
        path = self.base / "absent" / "db"
        store = Store(path, readonly=True)
        self.assertIsNone(store.setting("agent"))
        with self.assertRaises(Error):
            store.set_setting("agent", "pi")
        store.close()
        self.assertFalse(path.parent.exists())

    def test_linear_pagination_identity_and_priority(self):
        fixture = json.loads(self.fixture.read_text())
        fixture["issues"] += [dict(ISSUE, id="issue-2", identifier="ENG-124", priority=1)]
        fixture["projects"] += [{"id": "project-2", "name": "Project"}]
        self.fixture.write_text(json.dumps(fixture))
        linear = Linear()
        self.assertEqual(linear.workspace, "ws-uuid")
        self.assertEqual([i["priority"] for i in linear.mine()], [1, 0])
        self.assertEqual(len(linear.projects()), 2)
        self.assertEqual(linear.issue("ENG-123")["id"], ISSUE["id"])
        fixture["fail"] = True
        self.fixture.write_text(json.dumps(fixture))
        with self.assertRaises(Error):
            Linear()

    def test_linear_repeated_cursor_and_errors(self):
        linear = Linear()
        with patch.object(
            linear,
            "query",
            return_value={
                "issues": {"nodes": [], "pageInfo": {"hasNextPage": True, "endCursor": "same"}}
            },
        ):
            with self.assertRaisesRegex(Error, "重复"):
                linear.pages("ignored", "issues")
        with patch(
            "worktree_run.linear.execute",
            return_value=SimpleNamespace(stdout='{"errors":[{"message":"denied"}]}'),
        ):
            with self.assertRaisesRegex(Error, "GraphQL"):
                linear.query("query")

    def test_fzf_cancel_has_no_execution_side_effects(self):
        with patch(
            "worktree_run.cli.subprocess.run",
            return_value=SimpleNamespace(returncode=130, stdout=""),
        ):
            self.assertIsNone(select_issue([ISSUE]))
        with patch("worktree_run.cli.select_issue", return_value=None):
            self.assertEqual(main(["--db", str(self.store.path)]), 0)
        self.assertEqual(self.store.all("SELECT * FROM runs"), [])
        self.assertEqual(self.store.all("SELECT * FROM issues"), [])
        self.assertFalse((self.base / "trees").exists())

    def test_dry_run_is_readonly(self):
        self.install_agent()
        self.store.set_setting("agent", "pi")
        before = self.store.path.read_bytes()
        result = self.cli("ENG-123", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        plan = json.loads(result.stdout)
        self.assertEqual(plan["action"], "create")
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertFalse((self.base / "trees").exists())
        self.assertFalse(prompt.prompt_path("ws-uuid", "project-1").exists())
        self.assertFalse((self.base / "tmux.sock").exists())
        self.assertEqual(len(worktree.worktrees(self.repo)), 1)

    def test_config_crud_preserves_history(self):
        self.assertEqual(self.cli("config", "set", "agent", "pi").returncode, 0)
        self.assertEqual(self.store.setting("agent"), "pi")
        self.assertEqual(self.cli("config", "set", "agent", "unknown").returncode, 1)
        self.assertEqual(self.store.setting("agent"), "pi")
        self.assertEqual(self.cli("config", "set", "tmux-session", "invalid:name").returncode, 1)
        self.assertEqual(self.cli("project", "remove", "project-1").returncode, 0)
        self.assertTrue(self.repo.exists())
        self.assertEqual(
            self.cli(
                "project",
                "add",
                "project-1",
                "--baseline",
                "main",
                "--root",
                str(self.base / "new trees"),
                cwd=self.repo,
            ).returncode,
            0,
        )

    def test_synchronous_checkout_hook_finishes_before_launch(self):
        hooks = self.base / "hooks"
        hooks.mkdir()
        hook = hooks / "post-checkout"
        hook.write_text("#!/bin/sh\nsleep 0.1\nprintf ready > .environment-ready\n")
        hook.chmod(0o755)
        worktree.git(self.repo, "config", "core.hooksPath", str(hooks))
        row = self.row()
        worktree.prepare(row, associated=False)
        self.assertEqual((Path(row["worktree"]) / ".environment-ready").read_text(), "ready")
        self.assertFalse((self.repo / ".environment-ready").exists())

    def test_tmux_failure_preserves_worktree_and_allows_retry(self):
        self.install_agent()
        with (
            self.store.lock(),
            patch("worktree_run.tmux.Tmux.create", side_effect=Error("tmux failed")),
        ):
            with self.assertRaisesRegex(Error, "tmux failed"):
                runner.launch(self.store, self.context(), ISSUE, self.binding, "pi")
        row = self.store.issue("ws-uuid", ISSUE["id"])
        self.assertEqual(worktree.inspect(row, associated=True), "reuse")
        self.assertEqual(self.store.one("SELECT * FROM runs")["status"], "failed")
        self.assertIsNone(self.store.active("ws-uuid", ISSUE["id"]))

    def test_removed_configs_preserve_worktree_and_history(self):
        row = self.row()
        self.store.save_issue(row)
        worktree.prepare(row, associated=False)
        run_id = self.store.new_run(row, "pi", ["pi", "prompt"], None)
        self.store.update_run(run_id, status="failed")
        self.assertEqual(self.cli("project", "remove", "project-1").returncode, 0)
        self.assertEqual(self.cli("config", "set", "agent", "codex").returncode, 0)
        self.assertEqual(self.store.run(run_id)["agent"], "pi")
        self.assertEqual(worktree.inspect(row, associated=True), "reuse")

    def test_no_project_fallback_is_not_persisted(self):
        from worktree_run.cli import resolve_binding

        issue = dict(ISSUE, project=None)
        with (
            patch(
                "worktree_run.cli.binding_values",
                return_value={k: self.binding[k] for k in ("repo", "baseline", "root")},
            ),
            patch("worktree_run.cli.ask") as ask,
        ):
            binding = resolve_binding(self.store, self.context(), issue, False)
            ask.assert_not_called()
        self.assertEqual(binding["repo"], str(self.repo))
        self.assertEqual(len(self.store.all("SELECT * FROM projects")), 1)
        with self.assertRaisesRegex(Error, "未绑定"):
            resolve_binding(self.store, self.context(), issue, True)

    def test_failed_preparation_is_recorded(self):
        self.install_agent()
        row = self.row()
        with (
            self.store.lock(),
            patch("worktree_run.worktree.prepare", side_effect=Error("fetch failed")),
        ):
            with self.assertRaisesRegex(Error, "fetch failed"):
                runner.launch(self.store, self.context(), ISSUE, self.binding, "pi")
        run = self.store.one("SELECT * FROM runs")
        self.assertEqual(run["status"], "failed")
        self.assertEqual(self.store.issue("ws-uuid", ISSUE["id"]), row)
