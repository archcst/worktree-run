import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from worktree_run.store import Store
from worktree_run.worktree import git

ISSUE = dict(
    id="issue-uuid-1",
    identifier="ENG-123",
    title="中文 title 'quoted'",
    description="详情\n第二行",
    priority=0,
    createdAt="2026-01-01",
    project={"id": "project-1", "name": "Project"},
    state={"name": "Todo", "type": "unstarted"},
    assignee={"id": "me"},
)

LINEAR_SCRIPT = """#!PYTHON
import json, sys
from pathlib import Path
args = sys.argv[1:]
fixture = json.loads(Path(__file__).with_name("linear.json").read_text())
if fixture.get("fail"):
    print("Linear unavailable", file=sys.stderr); sys.exit(2)
query = args[1]
variables = json.loads(args[args.index("--variables-json") + 1])
if "viewer" in query:
    data = {"viewer": {"id":"me"}, "organization": {"id": "ws-uuid", "urlKey":"test-space"}}
elif "organization" in query:
    assert args[args.index("--workspace")+1] == "test-space"
    data = {"organization":{"id":"ws-uuid"}}
elif "issue(id:" in query:
    data = {"issue": fixture["issues"][0]}
else:
    field = "issues" if "issues(" in query else "projects"
    records = fixture[field]
    cursor = variables.get("after")
    page = int(cursor or 0)
    data = {field: {"nodes": records[page:page+1], "pageInfo": {
        "hasNextPage": page+1 < len(records), "endCursor": str(page+1)}}}
print(json.dumps({"data": data}))
"""

AGENT_SCRIPT = """#!PYTHON
import json, os, sys, time
from pathlib import Path
config = json.loads(Path(__file__).with_suffix('.json').read_text())
path, delay, code = config['output'], config['delay'], config['code']
Path(path).write_text(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd(),
    "workspace": os.environ.get("WTR_LINEAR_WORKSPACE"), "tty": sys.stdin.isatty()}))
print("AGENT STARTED", flush=True)
time.sleep(float(delay))
print("AGENT FINISHED", flush=True)
sys.exit(int(code))
"""


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wtr-test-")
        self.base = Path(self.temp.name)
        self.env = patch.dict(
            os.environ,
            {
                "XDG_CONFIG_HOME": str(self.base / "config"),
                "XDG_DATA_HOME": str(self.base / "data"),
                "WTR_TMUX_SOCKET": str(self.base / "tmux.sock"),
                "WTR_TMUX_CONFIG": "/dev/null",
                "PATH": str(self.base) + os.pathsep + os.environ["PATH"],
            },
        )
        self.env.start()
        self.repo = self.base / "main repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.repo)], check=True)
        git(self.repo, "config", "user.email", "test@example.invalid")
        git(self.repo, "config", "user.name", "Test")
        git(self.repo, "config", "commit.gpgsign", "false")
        git(self.repo, "config", "core.hooksPath", "/dev/null")
        (self.repo / "file.txt").write_text("initial\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "initial")
        self.store = Store(self.base / "state.db")
        self.binding = dict(
            workspace="ws-uuid",
            workspace_slug="test-space",
            project_id="project-1",
            name="Project",
            repo=str(self.repo),
            baseline="main",
            root=str(self.base / "trees"),
        )
        self.store.save_project(self.binding)
        self.fixture = self.base / "linear.json"
        self.fixture.write_text(
            json.dumps({"issues": [ISSUE], "projects": [{"id": "project-1", "name": "Project"}]})
        )
        fake = self.base / "linear"
        fake.write_text(LINEAR_SCRIPT.replace("#!PYTHON", "#!" + sys.executable))
        fake.chmod(0o755)

    def install_agent(self, name="pi", *, delay="0", code="0"):
        executable = self.base / name
        executable.write_text(AGENT_SCRIPT.replace("#!PYTHON", "#!" + sys.executable))
        executable.chmod(0o755)
        output = self.base / (name + " output.json")
        executable.with_suffix(".json").write_text(
            json.dumps(dict(output=str(output), delay=delay, code=code))
        )
        return output

    def tearDown(self):
        subprocess.run(
            ["tmux", "-S", str(self.base / "tmux.sock"), "kill-server"], capture_output=True
        )
        self.store.close()
        self.env.stop()
        self.temp.cleanup()

    def wait_for(self, predicate, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = predicate()
            if result:
                return result
            time.sleep(0.05)
        self.fail("timed out waiting for condition")

    def cli(self, *args, cwd=None):
        return subprocess.run(
            [sys.executable, "-m", "worktree_run", "--db", str(self.store.path), *args],
            text=True,
            capture_output=True,
            cwd=cwd or self.base,
        )
