import io
import json
from contextlib import redirect_stderr
from unittest.mock import patch

from support import ISSUE, Sandbox

from worktree_run.cli import main
from worktree_run.linear import TERMINAL_STATE_TYPES, Linear


class LinearFilterTests(Sandbox):
    def set_issues(self, issues):
        fixture = json.loads(self.fixture.read_text())
        fixture["issues"] = issues
        self.fixture.write_text(json.dumps(fixture))

    def test_mine_includes_only_open_issues_assigned_to_current_user(self):
        open_types = ("triage", "backlog", "unstarted", "started")
        issues = [
            dict(
                ISSUE,
                id=f"issue-{index}",
                identifier=f"ENG-{index}",
                state={"name": "Custom display name", "type": state},
            )
            for index, state in enumerate((*open_types, *TERMINAL_STATE_TYPES), 1)
        ]
        issues += [
            dict(ISSUE, id="other-user", identifier="ENG-90", assignee={"id": "other"}),
            dict(ISSUE, id="unassigned", identifier="ENG-91", assignee=None),
        ]
        self.set_issues(issues)
        result = Linear().mine()
        self.assertEqual({issue["state"]["type"] for issue in result}, set(open_types))
        self.assertEqual(len(result), len(open_types))

    def test_every_api_page_excludes_terminal_types(self):
        self.set_issues([ISSUE, dict(ISSUE, id="second", identifier="ENG-124")])
        linear = Linear()
        with patch.object(linear, "query", wraps=linear.query) as query:
            self.assertEqual(len(linear.mine()), 2)
        self.assertEqual(query.call_count, 2)
        self.assertEqual([call.args[1]["after"] for call in query.call_args_list], [None, "1"])
        for call in query.call_args_list:
            self.assertIn("nin: $closedTypes", call.args[0])
            self.assertEqual(
                set(call.args[1]["closedTypes"]), {"completed", "canceled", "duplicate"}
            )

    def test_selected_issue_becoming_duplicate_is_rejected_before_launch(self):
        self.set_issues([dict(ISSUE, state={"name": "Duplicate", "type": "duplicate"})])
        errors = io.StringIO()
        with (
            patch("worktree_run.cli.Linear.mine", return_value=[ISSUE]),
            patch("worktree_run.cli.select_issue", return_value=ISSUE),
            redirect_stderr(errors),
        ):
            result = main(["--db", str(self.store.path), "--agent", "pi"])
        self.assertEqual(result, 1)
        self.assertIn("状态已变化", errors.getvalue())
        self.assertEqual(self.store.all("SELECT * FROM runs"), [])
        self.assertEqual(self.store.all("SELECT * FROM issues"), [])
        self.assertFalse((self.base / "trees").exists())

    def test_explicit_issue_id_remains_accessible(self):
        self.set_issues([dict(ISSUE, state={"name": "Duplicate", "type": "duplicate"})])
        self.assertEqual(Linear().mine(), [])
        self.assertEqual(Linear().issue("ENG-123")["state"]["type"], "duplicate")
        self.install_agent()
        result = self.cli("ENG-123", "--agent", "pi", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["issue"]["identifier"], "ENG-123")
        self.assertEqual(self.store.all("SELECT * FROM runs"), [])
