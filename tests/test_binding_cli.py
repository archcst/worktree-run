import json
import shlex
import sys
from types import SimpleNamespace
from unittest.mock import patch

from support import ISSUE, Sandbox

from worktree_run import cli, prompt
from worktree_run.cli import ask
from worktree_run.tmux import Tmux
from worktree_run.util import Error
from worktree_run.worktree import git


class BindingAddTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.store.remove_binding(*cli.bindings.key(self.binding))

    def add(self, cwd):
        return self.cli(
            "binding",
            "add",
            "--project",
            "project-1",
            "--baseline",
            "main",
            "--root",
            str(self.base / "issue trees"),
            cwd=cwd,
        )

    def assert_rejected(self, cwd):
        # Local validation must happen before any Linear request or prompt.
        fixture = json.loads(self.fixture.read_text())
        fixture["fail"] = True
        self.fixture.write_text(json.dumps(fixture))
        result = self.add(cwd)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("main worktree", result.stderr)
        self.assertNotIn("Linear unavailable", result.stderr)
        self.assertEqual(self.store.bindings(), [])
        return result

    def test_project_add_accepts_local_name_and_lists_disabled_projects(self):
        result = self.cli(
            "project",
            "add",
            "--project",
            "project-1",
            "--name",
            "Local App",
            "--baseline",
            "main",
            "--root",
            str(self.base / "trees"),
            cwd=self.repo,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        row = json.loads(result.stdout)
        self.assertEqual(row["name"], "Local App")
        self.assertEqual(row["target_name"], "Project")
        self.assertNotEqual(row["project_id"], "project-1")
        listing = self.cli("project", "list")
        self.assertEqual(listing.returncode, 0, listing.stderr)
        self.assertEqual({r["enabled"] for r in json.loads(listing.stdout)}, {0, 1})

    def test_binds_current_main_root_without_path_prompt(self):
        result = self.add(self.repo)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["repo"], str(self.repo.resolve()))
        self.assertEqual(
            self.store.binding("ws-uuid", "project", "project-1")["repo"], str(self.repo.resolve())
        )

    def test_rejects_subdirectory_with_main_root_hint(self):
        subdir = self.repo / "src"
        subdir.mkdir()
        result = self.assert_rejected(subdir)
        self.assertIn(str(self.repo.resolve()), result.stderr)

    def test_rejects_linked_worktree_even_on_main_branch(self):
        git(self.repo, "checkout", "-b", "development")
        linked = self.base / "linked"
        git(self.repo, "worktree", "add", str(linked), "main")
        result = self.assert_rejected(linked)
        self.assertIn(str(self.repo.resolve()), result.stderr)

    def test_accepts_main_worktree_on_other_branch(self):
        git(self.repo, "checkout", "-b", "development")
        result = self.add(self.repo)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_rejects_non_repository(self):
        self.assert_rejected(self.base)

    def test_rejects_bare_repository(self):
        bare = self.base / "bare.git"
        git(self.repo, "clone", "--bare", str(self.repo), str(bare))
        self.assert_rejected(bare)

    def test_rejects_linked_worktree_of_bare_repository(self):
        bare = self.base / "bare.git"
        linked = self.base / "linked"
        git(self.repo, "clone", "--bare", str(self.repo), str(bare))
        git(bare, "worktree", "add", str(linked), "main")
        self.assert_rejected(linked)

    def test_accepts_separate_git_directory(self):
        git(self.repo, "init", "--separate-git-dir", str(self.base / "metadata"))
        result = self.add(self.repo)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_repo_flag_cannot_override_current_directory(self):
        result = self.cli("binding", "add", "--project", "project-1", "--repo", str(self.repo))
        self.assertEqual(result.returncode, 2)
        self.assertIn("--repo", result.stderr)
        self.assertEqual(self.store.bindings(), [])

    def root_prompt(self, *, action="add", expected=None):
        expected = expected or str(self.repo.resolve())
        command = [
            sys.executable,
            "-m",
            "worktree_run",
            "--db",
            str(self.store.path),
            "binding",
            action,
            *(["--project"] if action == "add" else []),
            "project-1",
            "--baseline",
            "main",
        ]
        if action == "edit":
            command += ["--repo", str(self.repo)]
        tmux = Tmux()
        pane = tmux.call(
            "new-session",
            "-d",
            "-P",
            "-F",
            "#{pane_id}",
            "-s",
            "project-input",
            "-x",
            "240",
            "-y",
            "30",
            "-c",
            str(self.repo),
            shlex.join(command),
        ).stdout.strip()
        tmux.call("set-option", "-w", "-t", pane, "remain-on-exit", "on")
        self.wait_for(lambda: expected in tmux.call("capture-pane", "-p", "-t", pane).stdout)
        return tmux, pane, expected

    def test_root_prefill_can_be_accepted_with_enter(self):
        tmux, pane, expected = self.root_prompt()
        tmux.call("send-keys", "-t", pane, "Enter")
        binding = self.wait_for(lambda: self.store.binding("ws-uuid", "project", "project-1"))
        self.assertEqual(binding["root"], expected)
        self.assertEqual(self.store.all("SELECT * FROM issues"), [])

    def test_root_prefill_can_be_edited_in_place(self):
        tmux, pane, expected = self.root_prompt()
        tmux.call("send-keys", "-t", pane, "BSpace")
        tmux.call("send-keys", "-l", "-t", pane, "--", "-中文 custom")
        tmux.call("send-keys", "-t", pane, "Enter")
        binding = self.wait_for(lambda: self.store.binding("ws-uuid", "project", "project-1"))
        self.assertEqual(binding["root"], expected[:-1] + "-中文 custom")

    def test_edit_prefills_saved_root(self):
        self.store.save_binding(self.binding)
        tmux, pane, _ = self.root_prompt(action="edit", expected=self.binding["root"])
        tmux.call("send-keys", "-t", pane, "Enter")
        self.wait_for(
            lambda: (
                tmux.call("display-message", "-p", "-t", pane, "#{pane_dead}").stdout.strip() == "1"
            )
        )
        self.assertEqual(
            self.store.binding("ws-uuid", "project", "project-1")["root"],
            str(self.base.resolve() / "trees"),
        )

    def test_cancel_root_prompt_does_not_bind_project(self):
        tmux, pane, _ = self.root_prompt()
        tmux.call("send-keys", "-t", pane, "C-c")
        self.wait_for(
            lambda: (
                tmux.call("display-message", "-p", "-t", pane, "#{pane_dead}").stdout.strip() == "1"
            )
        )
        self.assertEqual(self.store.bindings(), [])

    def test_editable_prompt_passes_default_and_returns_edited_path(self):
        default = str(self.repo.resolve())
        with (
            patch("sys.stdin.isatty", return_value=True),
            patch("prompt_toolkit.prompt", return_value="/tmp/custom trees") as input_prompt,
        ):
            self.assertEqual(ask("root", default, editable=True), "/tmp/custom trees")
        self.assertEqual(input_prompt.call_args.kwargs["default"], default)

    def test_target_change_preserves_local_identity_template_and_overrides(self):
        original = dict(self.binding, name="Local App", agent="codex", tmux_session="local-session")
        self.store.save_binding(original)
        path = prompt.initialize(original["workspace"], original["project_id"])
        path.write_text("Local template {{issue_id}}")
        result = self.cli("project", "edit", "Local App", "--label", "label-1")
        self.assertEqual(result.returncode, 0, result.stderr)
        row = json.loads(result.stdout)
        for key in ("project_id", "name", "repo", "baseline", "root", "agent", "tmux_session"):
            self.assertEqual(row[key], original[key])
        self.assertEqual(row["target_name"], "repo:app")
        self.assertEqual(row["kind"], "label")
        self.assertEqual(path.read_text(), "Local template {{issue_id}}")
        self.assertIsNone(self.store.binding("ws-uuid", "project", "project-1"))
        result = self.cli("project", "edit", row["project_id"], "--name", "Renamed App")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["name"], "Renamed App")
        self.assertEqual(path.read_text(), "Local template {{issue_id}}")

    def test_removing_disabled_project_does_not_disable_new_target_owner(self):
        result = self.add(self.repo)
        self.assertEqual(result.returncode, 0, result.stderr)
        current = json.loads(result.stdout)
        result = self.cli("project", "remove", self.binding["project_id"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.store.binding("ws-uuid", "project", "project-1"), current)
        self.assertEqual(self.store.project("ws-uuid", self.binding["project_id"])["enabled"], 0)

    def test_disabled_project_target_change_reenables_matching(self):
        result = self.cli("project", "edit", self.binding["project_id"], "--label", "label-1")
        self.assertEqual(result.returncode, 0, result.stderr)
        row = json.loads(result.stdout)
        self.assertEqual(row["project_id"], self.binding["project_id"])
        self.assertEqual(row["enabled"], 1)
        self.assertEqual(self.store.binding("ws-uuid", "label", "label-1"), row)

    def test_edit_remains_available_from_other_directories(self):
        self.assertEqual(self.add(self.repo).returncode, 0)
        result = self.cli(
            "binding",
            "edit",
            "project-1",
            "--repo",
            str(self.repo),
            "--baseline",
            "main",
            "--root",
            str(self.base / "edited trees"),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout)["root"], str((self.base / "edited trees").resolve())
        )


class LocalProjectTests(Sandbox):
    def test_prompt_selection_uses_local_name_or_id_without_linear(self):
        with patch.object(cli, "Linear") as linear:
            for name in (self.binding["name"], self.binding["project_id"]):
                row = cli.prompt_project(self.store, SimpleNamespace(workspace=None, project=name))
                self.assertEqual(row, self.binding)
            with self.assertRaisesRegex(Error, "本地 WTR 项目"):
                cli.prompt_project(self.store, SimpleNamespace(workspace=None, project="project-1"))
            with self.assertRaisesRegex(Error, "创建"):
                cli.prompt_project(self.store, SimpleNamespace(workspace="missing", project=None))
        linear.assert_not_called()

    def test_prompt_infers_local_label_project_in_repository_and_issue_worktree(self):
        local = dict(self.binding, kind="label", target_id="label-1", target_name="repo:app")
        self.store.replace_binding(self.binding, local)
        issue = cli.worktree.route(
            self.store, SimpleNamespace(workspace="ws-uuid", slug="test-space"), ISSUE, local
        )
        self.store.save_issue(issue)
        args = SimpleNamespace(workspace=None, project=None)
        with patch.object(cli, "Linear") as linear:
            for cwd in (self.repo, self.repo / "src", cli.Path(issue["worktree"]) / "src"):
                with patch.object(cli.Path, "cwd", return_value=cwd):
                    self.assertEqual(
                        cli.prompt_project(self.store, args)["project_id"], local["project_id"]
                    )
        linear.assert_not_called()

    def test_associated_issue_resolves_disabled_local_project_not_issue_row(self):
        linear = SimpleNamespace(workspace="ws-uuid", slug="test-space")
        issue_row = cli.worktree.route(self.store, linear, ISSUE, self.binding)
        self.store.save_issue(issue_row)
        self.store.remove_binding(*cli.bindings.key(self.binding))
        changed_issue = dict(ISSUE, project={"id": "different-remote", "name": "Different"})
        result = cli.resolve_binding(self.store, linear, changed_issue, True)
        self.assertEqual(result["project_id"], "wtr-project-1")
        self.assertEqual(result["enabled"], 0)
        self.assertNotIn("issue_id", result)
        with patch.object(self.store, "project", return_value=None):
            with self.assertRaisesRegex(Error, "本地 WTR 项目不存在"):
                cli.resolve_binding(self.store, linear, changed_issue, True)

    def test_agent_precedence_override_project_global_and_interactive_choice(self):
        self.store.set_setting("agent", "pi")
        project = dict(self.binding, agent="codex")
        self.assertEqual(cli.selected_agent(self.store, None, project), "codex")
        self.assertEqual(cli.selected_agent(self.store, "claude", project), "claude")
        self.assertEqual(cli.selected_agent(self.store, None, self.binding), "pi")
        self.assertEqual(cli.selected_agent(self.store, None), "pi")
        with (
            patch.object(cli.agents, "catalog", return_value=[dict(name="pi", installed=True)]),
            patch.object(cli, "choose", return_value={"name": "pi"}) as choose,
            patch("sys.stdin.isatty", return_value=True),
        ):
            self.assertEqual(cli.selected_agent(self.store, "", project), "pi")
        choose.assert_called_once()

    def test_start_dry_run_passes_project_execution_overrides(self):
        self.install_agent("pi")
        self.install_agent("codex")
        self.store.set_setting("agent", "pi")
        self.store.set_setting("tmux-session", "global-session")
        self.store.save_binding(dict(self.binding, agent="codex", tmux_session="project-session"))
        result = self.cli(ISSUE["identifier"], "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        plan = json.loads(result.stdout)
        self.assertEqual(plan["agent"], "codex")
        self.assertEqual(plan["tmux_session"], "project-session")
        result = self.cli(ISSUE["identifier"], "--dry-run", "--agent", "pi")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["agent"], "pi")
        self.assertEqual(self.store.all("SELECT * FROM runs"), [])

    def test_start_launch_receives_local_project_agent(self):
        project = dict(self.binding, agent="codex")
        self.store.save_binding(project)
        run = dict(
            id="run",
            status="running",
            session_id="s",
            window_id="w",
            pane_id="p",
            tmux_socket="socket",
        )
        args = SimpleNamespace(
            workspace=None, issue="ENG-1", dry_run=False, agent=None, no_attach=True
        )
        with (
            patch.object(cli, "Linear") as linear,
            patch.object(cli.runner, "recover"),
            patch.object(cli.runner, "launch", return_value=run) as launch,
            patch.object(cli, "output"),
        ):
            linear.return_value.workspace = "ws-uuid"
            linear.return_value.issue.return_value = ISSUE
            cli.start(self.store, args)
        self.assertEqual(launch.call_args.args[-2:], (project, "codex"))
