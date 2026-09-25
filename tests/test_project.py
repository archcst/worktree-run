import json
import shlex
import sys
from unittest.mock import patch

from support import Sandbox

from worktree_run.cli import ask
from worktree_run.tmux import Tmux
from worktree_run.worktree import git


class ProjectAddTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.store.write("DELETE FROM projects")

    def add(self, cwd):
        return self.cli(
            "project",
            "add",
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
        self.assertEqual(self.store.all("SELECT * FROM projects"), [])
        return result

    def test_binds_current_main_root_without_path_prompt(self):
        result = self.add(self.repo)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["repo"], str(self.repo.resolve()))
        self.assertEqual(
            self.store.project("ws-uuid", "project-1")["repo"], str(self.repo.resolve())
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
        result = self.cli("project", "add", "project-1", "--repo", str(self.repo))
        self.assertEqual(result.returncode, 2)
        self.assertIn("--repo", result.stderr)
        self.assertEqual(self.store.all("SELECT * FROM projects"), [])

    def root_prompt(self, *, action="add", expected=None):
        expected = expected or str(self.repo.resolve())
        command = [
            sys.executable,
            "-m",
            "worktree_run",
            "--db",
            str(self.store.path),
            "project",
            action,
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
        binding = self.wait_for(lambda: self.store.project("ws-uuid", "project-1"))
        self.assertEqual(binding["root"], expected)
        self.assertEqual(self.store.all("SELECT * FROM issues"), [])

    def test_root_prefill_can_be_edited_in_place(self):
        tmux, pane, expected = self.root_prompt()
        tmux.call("send-keys", "-t", pane, "BSpace")
        tmux.call("send-keys", "-l", "-t", pane, "--", "-中文 custom")
        tmux.call("send-keys", "-t", pane, "Enter")
        binding = self.wait_for(lambda: self.store.project("ws-uuid", "project-1"))
        self.assertEqual(binding["root"], expected[:-1] + "-中文 custom")

    def test_edit_prefills_saved_root(self):
        self.store.save_project(self.binding)
        tmux, pane, _ = self.root_prompt(action="edit", expected=self.binding["root"])
        tmux.call("send-keys", "-t", pane, "Enter")
        self.wait_for(
            lambda: (
                tmux.call("display-message", "-p", "-t", pane, "#{pane_dead}").stdout.strip() == "1"
            )
        )
        self.assertEqual(
            self.store.project("ws-uuid", "project-1")["root"], str(self.base.resolve() / "trees")
        )

    def test_cancel_root_prompt_does_not_bind_project(self):
        tmux, pane, _ = self.root_prompt()
        tmux.call("send-keys", "-t", pane, "C-c")
        self.wait_for(
            lambda: (
                tmux.call("display-message", "-p", "-t", pane, "#{pane_dead}").stdout.strip() == "1"
            )
        )
        self.assertEqual(self.store.all("SELECT * FROM projects"), [])

    def test_editable_prompt_passes_default_and_returns_edited_path(self):
        default = str(self.repo.resolve())
        with (
            patch("sys.stdin.isatty", return_value=True),
            patch("prompt_toolkit.prompt", return_value="/tmp/custom trees") as input_prompt,
        ):
            self.assertEqual(ask("root", default, editable=True), "/tmp/custom trees")
        self.assertEqual(input_prompt.call_args.kwargs["default"], default)

    def test_edit_remains_available_from_other_directories(self):
        self.assertEqual(self.add(self.repo).returncode, 0)
        result = self.cli(
            "project",
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
