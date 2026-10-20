"""Versioned SQLite storage and a single-host orchestration lock."""

import fcntl
import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .util import Error

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS bindings (
 workspace TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('project','label')),
 target_id TEXT NOT NULL, name TEXT NOT NULL, workspace_slug TEXT NOT NULL,
 team_name TEXT NOT NULL, repo TEXT NOT NULL, baseline TEXT NOT NULL, root TEXT NOT NULL,
 PRIMARY KEY(workspace, kind, target_id)
);
CREATE TABLE IF NOT EXISTS prompt_projects (
 workspace TEXT NOT NULL, project_id TEXT NOT NULL, name TEXT NOT NULL,
 workspace_slug TEXT NOT NULL, PRIMARY KEY(workspace, project_id)
);
CREATE TABLE IF NOT EXISTS issues (
 workspace TEXT NOT NULL, issue_id TEXT NOT NULL, identifier TEXT NOT NULL,
 workspace_slug TEXT NOT NULL, project_id TEXT, repo TEXT NOT NULL,
 baseline TEXT NOT NULL, branch TEXT NOT NULL, worktree TEXT NOT NULL UNIQUE,
 PRIMARY KEY(workspace, issue_id), UNIQUE(repo, branch)
);
CREATE TABLE IF NOT EXISTS runs (
 id TEXT PRIMARY KEY, workspace TEXT NOT NULL, issue_id TEXT NOT NULL,
 identifier TEXT NOT NULL, agent TEXT NOT NULL, argv TEXT NOT NULL,
 cwd TEXT NOT NULL, workspace_slug TEXT NOT NULL, status TEXT NOT NULL
 CHECK(status IN ('preparing','running','awaiting_review','failed','interrupted')),
 created_at TEXT NOT NULL, started_at TEXT, ended_at TEXT, exit_code INTEGER,
 error TEXT, session_id TEXT, window_id TEXT, pane_id TEXT,
 tmux_socket TEXT, launch_command TEXT, agent_session TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS active_issue ON runs(workspace, issue_id)
 WHERE status IN ('preparing','running');
PRAGMA user_version = 2;
"""


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
        if readonly and self.path.exists():
            self.db = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=30)
        else:
            if not readonly:
                self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.db = sqlite3.connect(":memory:" if readonly else self.path, timeout=30)
        self.db.row_factory = sqlite3.Row
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version > 2 or (readonly and self.path.exists() and version < 2):
            self.db.close()
            if version > 2:
                raise Error("数据库版本高于当前 wtr，请升级")
            raise Error("请先运行 wtr config 配置仓库绑定，再使用只读预览")
        if not readonly or not self.path.exists():
            self.db.executescript(SCHEMA)
            self.db.commit()
            if not readonly:
                self.path.chmod(0o600)

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
        lock_path = self.path.with_suffix(".lock")
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
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
            "SELECT * FROM bindings WHERE (? IS NULL OR workspace_slug=?) "
            "ORDER BY workspace_slug,kind,name,target_id",
            (workspace, workspace),
        )

    def binding(self, workspace, kind, target_id):
        return self.one(
            "SELECT * FROM bindings WHERE workspace=? AND kind=? AND target_id=?",
            (workspace, kind, target_id),
        )

    def save_binding(self, row):
        self.write(
            """INSERT INTO bindings
            (workspace,kind,target_id,name,workspace_slug,team_name,repo,baseline,root)
            VALUES (:workspace,:kind,:target_id,:name,:workspace_slug,:team_name,:repo,:baseline,:root)
            ON CONFLICT(workspace,kind,target_id) DO UPDATE SET name=excluded.name,
            workspace_slug=excluded.workspace_slug,team_name=excluded.team_name,
            repo=excluded.repo,baseline=excluded.baseline,root=excluded.root""",
            row,
        )
        if row["kind"] == "project":
            self.save_project(dict(row, project_id=row["target_id"]))

    def replace_binding(self, previous, row):
        """Replace a binding atomically after the caller's interactive work is complete."""
        if self.readonly:
            raise Error("只读模式不能修改数据库")
        if previous["workspace"] != row["workspace"]:
            raise Error("不能将仓库绑定移到其他 workspace")
        old_key = (previous["workspace"], previous["kind"], previous["target_id"])
        new_key = (row["workspace"], row["kind"], row["target_id"])
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if self.binding(*old_key) != previous:
                raise Error("仓库绑定已被其他进程修改，请重新选择后再保存")
            if new_key != old_key and self.binding(*new_key):
                raise Error("目标 Project/Label 已被其他绑定占用，未修改原绑定")
            self.db.execute(
                """UPDATE bindings SET kind=:kind,target_id=:target_id,name=:name,
                workspace_slug=:workspace_slug,team_name=:team_name,repo=:repo,
                baseline=:baseline,root=:root
                WHERE workspace=:workspace AND kind=:old_kind AND target_id=:old_target_id""",
                dict(row, old_kind=previous["kind"], old_target_id=previous["target_id"]),
            )
            if row["kind"] == "project":
                self.db.execute(
                    """INSERT INTO prompt_projects (workspace,project_id,name,workspace_slug)
                    VALUES (:workspace,:target_id,:name,:workspace_slug)
                    ON CONFLICT(workspace,project_id) DO UPDATE SET name=excluded.name,
                    workspace_slug=excluded.workspace_slug""",
                    row,
                )

    def remove_binding(self, workspace, kind, target_id):
        self.write(
            "DELETE FROM bindings WHERE workspace=? AND kind=? AND target_id=?",
            (workspace, kind, target_id),
        )

    def projects(self, workspace=None):
        return self.all(
            "SELECT * FROM prompt_projects WHERE (? IS NULL OR workspace_slug=?) "
            "ORDER BY workspace_slug,name",
            (workspace, workspace),
        )

    def project(self, workspace, project_id):
        return self.one(
            "SELECT * FROM prompt_projects WHERE workspace=? AND project_id=?",
            (workspace, project_id),
        )

    def save_project(self, row):
        self.write(
            """INSERT INTO prompt_projects (workspace,project_id,name,workspace_slug)
            VALUES (:workspace,:project_id,:name,:workspace_slug)
            ON CONFLICT(workspace,project_id) DO UPDATE SET name=excluded.name,
            workspace_slug=excluded.workspace_slug""",
            row,
        )

    def issue(self, workspace, issue_id):
        return self.one(
            "SELECT * FROM issues WHERE workspace=? AND issue_id=?", (workspace, issue_id)
        )

    def save_issue(self, row):
        # The association is reserved before Git runs. A failure remains retryable.
        self.write(
            """INSERT INTO issues VALUES (:workspace,:issue_id,:identifier,:workspace_slug,
            :project_id,:repo,:baseline,:branch,:worktree)
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
            (id,workspace,issue_id,identifier,agent,argv,cwd,workspace_slug,status,created_at,tmux_socket)
            VALUES (?,?,?,?,?,?,?,?, 'preparing',?,?)""",
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
