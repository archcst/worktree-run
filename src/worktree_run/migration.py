"""Backed-up, local-only upgrade to stable WTR project ownership."""

import os
import re
import sqlite3
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import prompt
from .store import PROJECT_SCHEMA, PROJECT_UPSERT
from .util import Error


def legacy_template(workspace, project_id):
    if not all(
        isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]+", value)
        for value in (workspace, project_id)
    ):
        raise Error("旧提示词身份无效，无法安全迁移")
    return prompt.directory() / workspace / f"{project_id}.md"


def same_repo(left, right):
    return Path(left).expanduser().resolve() == Path(right).expanduser().resolve()


def source_for(binding, legacy, issues):
    """Use an explicit old project link, or one unambiguous customized template."""
    candidates = [
        row
        for row in legacy
        if row["workspace"] == binding["workspace"] and same_repo(row["repo"], binding["repo"])
    ]
    local_name = candidates[0]["name"] if len(candidates) == 1 else binding["name"]
    identities = {row["project_id"] for row in candidates}
    if binding["kind"] == "project":
        identities.add(binding["target_id"])
    identities.update(
        row["project_id"]
        for row in issues
        if row.get("project_id")
        and row["workspace"] == binding["workspace"]
        and same_repo(row["repo"], binding["repo"])
    )
    customized = {}
    for project_id in sorted(identities):
        path = legacy_template(binding["workspace"], project_id)
        if path.exists():
            text = path.read_text(encoding="utf-8")
            if text != prompt.DEFAULT:
                customized.setdefault(text, path)
    if len(customized) > 1:
        raise Error(f"仓库 {binding['repo']} 存在多份不同的自定义提示词，请明确归属后再迁移")
    return local_name, next(iter(customized.values()), None)


def copy_template(project, source):
    text = source.read_text(encoding="utf-8") if source else prompt.DEFAULT
    prompt.validate(text, source or "内置默认模板")
    target = prompt.prompt_path(project["workspace"], project["project_id"])
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".wtr-template-", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            # Publish the complete file without replacing an existing destination.
            os.link(temporary, target)
        except FileExistsError:
            if target.read_text(encoding="utf-8") != text:
                raise Error(f"迁移目标提示词已有不同内容，未覆盖：{target}")
    finally:
        Path(temporary).unlink(missing_ok=True)


def migrate(store):
    """Caller holds Store.lock; originals remain intact and retries use stable IDs."""
    version = store.db.execute("PRAGMA user_version").fetchone()[0]
    backup = store.path.with_name(
        store.path.name
        + f".backup-v{version}-"
        + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    )
    fd = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    destination = sqlite3.connect(backup)
    try:
        store.db.backup(destination)
    finally:
        destination.close()
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        tables = {
            row["name"] for row in store.all("SELECT name FROM sqlite_master WHERE type='table'")
        }
        legacy = store.all("SELECT * FROM projects") if "projects" in tables else []
        old = store.all("SELECT * FROM bindings") if "bindings" in tables else []
        if "bindings" not in tables:
            old = [
                dict(row, kind="project", target_id=row["project_id"], team_name="")
                for row in legacy
            ]
        issues = store.all("SELECT * FROM issues")
        for statement in PROJECT_SCHEMA.split(";"):
            if statement.strip():
                store.db.execute(statement)
        for table in ("issues", "runs"):
            columns = {row["name"] for row in store.all(f"PRAGMA table_info({table})")}
            if "wtr_project_id" not in columns:
                store.db.execute(f"ALTER TABLE {table} ADD COLUMN wtr_project_id TEXT")
        projects = []

        def create(binding, enabled):
            name, source = source_for(binding, legacy, issues)
            identity = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"wtr:v3:{binding['workspace']}:{binding['kind']}:{binding['target_id']}",
                )
            )
            row = dict(
                workspace=binding["workspace"],
                workspace_slug=binding["workspace_slug"],
                project_id=identity,
                name=name,
                kind=binding["kind"],
                target_id=binding["target_id"],
                target_name=binding["name"],
                team_name=binding.get("team_name", ""),
                repo=binding["repo"],
                baseline=binding["baseline"],
                root=binding["root"],
                agent=None,
                tmux_session=None,
                enabled=enabled,
            )
            copy_template(row, source)
            store.db.execute(PROJECT_UPSERT, row)
            projects.append(row)
            return row

        for binding in old:
            create(binding, 1)
        for issue in issues:
            matches = [
                row
                for row in projects
                if row["workspace"] == issue["workspace"] and same_repo(row["repo"], issue["repo"])
            ]
            if len(matches) > 1:
                raise Error(
                    f"历史 issue {issue['identifier']} 对应多个 WTR 项目，未猜测归属；备份：{backup}"
                )
            if matches:
                project = matches[0]
            else:
                # A removed matching rule must not make historical worktrees lose their project.
                target = "historical-" + str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL, f"{issue['workspace']}:{Path(issue['repo']).resolve()}"
                    )
                )
                project = create(
                    dict(
                        workspace=issue["workspace"],
                        workspace_slug=issue["workspace_slug"],
                        kind="project",
                        target_id=target,
                        name=Path(issue["repo"]).name,
                        repo=issue["repo"],
                        baseline=issue["baseline"],
                        root=str(Path(issue["worktree"]).parent),
                    ),
                    0,
                )
            store.db.execute(
                "UPDATE issues SET wtr_project_id=? WHERE workspace=? AND issue_id=?",
                (project["project_id"], issue["workspace"], issue["issue_id"]),
            )
        store.db.execute("""UPDATE runs SET wtr_project_id=(
            SELECT i.wtr_project_id FROM issues i WHERE i.workspace=runs.workspace AND i.issue_id=runs.issue_id)
            WHERE wtr_project_id IS NULL""")
        store.db.execute("PRAGMA user_version=3")
    return backup
