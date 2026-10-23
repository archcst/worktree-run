"""SQLite storage for stable WTR projects, issue associations and execution history."""

import fcntl
import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .util import Error

PROJECT_SCHEMA = """
CREATE TABLE IF NOT EXISTS wtr_projects (
 workspace TEXT NOT NULL, project_id TEXT NOT NULL, name TEXT NOT NULL,
 workspace_slug TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('project','label')),
 target_id TEXT NOT NULL, target_name TEXT NOT NULL, team_name TEXT NOT NULL,
 repo TEXT NOT NULL, baseline TEXT NOT NULL, root TEXT NOT NULL,
 agent TEXT, tmux_session TEXT, enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
 PRIMARY KEY(workspace,project_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS active_target ON wtr_projects(workspace,kind,target_id)
 WHERE enabled=1;
"""
SCHEMA = (
    PROJECT_SCHEMA
    + """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS issues (
 workspace TEXT NOT NULL, issue_id TEXT NOT NULL, identifier TEXT NOT NULL,
 workspace_slug TEXT NOT NULL, project_id TEXT, repo TEXT NOT NULL,
 baseline TEXT NOT NULL, branch TEXT NOT NULL, worktree TEXT NOT NULL UNIQUE,
 wtr_project_id TEXT,
 PRIMARY KEY(workspace, issue_id), UNIQUE(repo, branch)
);
CREATE TABLE IF NOT EXISTS runs (
 id TEXT PRIMARY KEY, workspace TEXT NOT NULL, issue_id TEXT NOT NULL,
 identifier TEXT NOT NULL, agent TEXT NOT NULL, argv TEXT NOT NULL,
 cwd TEXT NOT NULL, workspace_slug TEXT NOT NULL, status TEXT NOT NULL
 CHECK(status IN ('preparing','running','awaiting_review','failed','interrupted')),
 created_at TEXT NOT NULL, started_at TEXT, ended_at TEXT, exit_code INTEGER,
 error TEXT, session_id TEXT, window_id TEXT, pane_id TEXT,
 tmux_socket TEXT, launch_command TEXT, agent_session TEXT, wtr_project_id TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS active_issue ON runs(workspace, issue_id)
 WHERE status IN ('preparing','running');
PRAGMA user_version = 3;
"""
)
PROJECT_UPSERT = """INSERT INTO wtr_projects
(workspace,project_id,name,workspace_slug,kind,target_id,target_name,team_name,
 repo,baseline,root,agent,tmux_session,enabled)
VALUES (:workspace,:project_id,:name,:workspace_slug,:kind,:target_id,:target_name,:team_name,
 :repo,:baseline,:root,:agent,:tmux_session,:enabled)
ON CONFLICT(workspace,project_id) DO UPDATE SET name=excluded.name,
workspace_slug=excluded.workspace_slug,kind=excluded.kind,target_id=excluded.target_id,
target_name=excluded.target_name,team_name=excluded.team_name,repo=excluded.repo,
baseline=excluded.baseline,root=excluded.root,agent=excluded.agent,
tmux_session=excluded.tmux_session,enabled=excluded.enabled"""


def now():
    return datetime.now(timezone.utc).isoformat()


def default_db():
    return (
        Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
        / "worktree-run/state.db"
    )


class Store:
    def __init__(self, path=None, *, readonly=False):
        self.path = Path(path or default_db()).expanduser().resolve()
        self.readonly = readonly
        self.migration_backup = None
        exists = self.path.exists()
        if readonly and exists:
            self.db = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=30)
        else:
            if not readonly:
                self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.db = sqlite3.connect(":memory:" if readonly else self.path, timeout=30)
        self.db.row_factory = sqlite3.Row
        try:
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            if version > 3:
                raise Error("数据库版本高于当前 wtr，请升级")
            if readonly and exists and version < 3:
                raise Error("请先运行 wtr config 升级 WTR 项目配置，再使用只读预览")
            if version in (1, 2):
                from .migration import migrate

                with self.lock():
                    # Another process may have finished the upgrade while this one waited.
                    if self.db.execute("PRAGMA user_version").fetchone()[0] < 3:
                        self.migration_backup = migrate(self)
            elif version == 0:
                self.db.executescript(SCHEMA)
            if not readonly:
                self.path.chmod(0o600)
        except BaseException:
            self.db.close()
            raise

    def close(self):
        self.db.close()

    def all(self, sql, params=()):
        return [dict(row) for row in self.db.execute(sql, params)]

    def one(self, sql, params=()):
        row = self.db.execute(sql, params).fetchone()
        return dict(row) if row else None

    def write(self, sql, params=()):
        if self.readonly:
            raise Error("只读模式不能修改数据库")
        with self.db:
            return self.db.execute(sql, params)

    @contextmanager
    def lock(self):
        if self.readonly:
            raise Error("只读模式不能认领执行任务")
        fd = os.open(self.path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(fd, "a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def setting(self, key, default=None):
        row = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return row["value"] if row else default

    def set_setting(self, key, value):
        self.write(
            "INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def bindings(self, workspace=None):
        return self.all(
            "SELECT * FROM wtr_projects WHERE enabled=1 AND (? IS NULL OR workspace_slug=?) "
            "ORDER BY workspace_slug,name,project_id",
            (workspace, workspace),
        )

    def projects(self, workspace=None):
        return self.all(
            "SELECT * FROM wtr_projects WHERE (? IS NULL OR workspace_slug=?) "
            "ORDER BY workspace_slug,name,project_id",
            (workspace, workspace),
        )

    def project(self, workspace, project_id):
        return self.one(
            "SELECT * FROM wtr_projects WHERE workspace=? AND project_id=?", (workspace, project_id)
        )

    def binding(self, workspace, kind, target_id):
        return self.one(
            "SELECT * FROM wtr_projects WHERE enabled=1 AND workspace=? AND kind=? AND target_id=?",
            (workspace, kind, target_id),
        )

    def save_binding(self, row):
        self.write(PROJECT_UPSERT, row)
        return self.project(row["workspace"], row["project_id"])

    def replace_binding(self, previous, row):
        """Caller holds the orchestration lock. Local project identity cannot change."""
        if self.readonly:
            raise Error("只读模式不能修改数据库")
        if (previous["workspace"], previous["project_id"]) != (row["workspace"], row["project_id"]):
            raise Error("不能改变 WTR 项目的身份或 workspace")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if self.project(previous["workspace"], previous["project_id"]) != previous:
                raise Error("WTR 项目已被其他进程修改，请重新选择后再保存")
            occupied = self.binding(row["workspace"], row["kind"], row["target_id"])
            if row["enabled"] and occupied and occupied["project_id"] != row["project_id"]:
                raise Error("目标 Project/Label 已被其他绑定占用，未修改原项目")
            self.db.execute(PROJECT_UPSERT, row)

    def remove_binding(self, workspace, kind, target_id):
        # Disable matching, but retain the project and its configuration for pinned issues.
        self.write(
            "UPDATE wtr_projects SET enabled=0 WHERE workspace=? AND kind=? AND target_id=?",
            (workspace, kind, target_id),
        )

    def issue(self, workspace, issue_id):
        return self.one(
            "SELECT * FROM issues WHERE workspace=? AND issue_id=?", (workspace, issue_id)
        )

    def save_issue(self, row):
        previous = self.issue(row["workspace"], row["issue_id"])
        if previous and previous["wtr_project_id"] != row["wtr_project_id"]:
            raise Error("已有 issue 的 WTR 项目关联不能自动改变")
        self.write(
            """INSERT INTO issues
            (workspace,issue_id,identifier,workspace_slug,project_id,repo,baseline,branch,worktree,wtr_project_id)
            VALUES (:workspace,:issue_id,:identifier,:workspace_slug,:project_id,:repo,:baseline,:branch,:worktree,:wtr_project_id)
            ON CONFLICT(workspace,issue_id) DO UPDATE SET identifier=excluded.identifier,
            workspace_slug=excluded.workspace_slug,project_id=excluded.project_id""",
            row,
        )

    def active(self, workspace, issue_id):
        return self.one(
            "SELECT * FROM runs WHERE workspace=? AND issue_id=? AND status IN ('preparing','running')",
            (workspace, issue_id),
        )

    def new_run(self, issue, agent, argv, socket):
        run_id = str(uuid.uuid4())
        self.write(
            """INSERT INTO runs
            (id,workspace,issue_id,identifier,agent,argv,cwd,workspace_slug,status,created_at,tmux_socket,wtr_project_id)
            VALUES (?,?,?,?,?,?,?,?, 'preparing',?,?,?)""",
            (
                run_id,
                issue["workspace"],
                issue["issue_id"],
                issue["identifier"],
                agent,
                json.dumps(argv, ensure_ascii=False),
                issue["worktree"],
                issue["workspace_slug"],
                now(),
                socket,
                issue["wtr_project_id"],
            ),
        )
        return run_id

    def run(self, run_id):
        return self.one("SELECT * FROM runs WHERE id=?", (run_id,))

    def update_run(self, run_id, **values):
        allowed = {
            "status",
            "started_at",
            "ended_at",
            "exit_code",
            "error",
            "session_id",
            "window_id",
            "pane_id",
            "launch_command",
            "agent_session",
        }
        if not values or not values.keys() <= allowed:
            raise ValueError("invalid run fields")
        self.write(
            f"UPDATE runs SET {','.join(key + '=?' for key in values)} WHERE id=?",
            (*values.values(), run_id),
        )
