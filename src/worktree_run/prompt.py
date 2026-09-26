"""Project-scoped prompt files, keyed by stable Linear identities."""

import os
import re
import shlex
import subprocess
from pathlib import Path

from .util import Error, clean, require

DEFAULT = """请开发 Linear issue {{issue_id}}。

通过 linear CLI 阅读需求和讨论，遵循仓库开发规范，
完成实现和必要测试，最后总结改动与验证结果。
"""


def directory():
    root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")).expanduser()
    return (root / "worktree-run/prompts").resolve()


def prompt_path(workspace_id, project_id):
    if project_id is None:
        return None
    for value in (workspace_id, project_id):
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
            raise Error("提示词路径需要有效的 Linear workspace/project ID")
    return (directory() / workspace_id / f"{project_id}.md").resolve()


def initialize(workspace_id, project_id):
    path = prompt_path(workspace_id, project_id)
    if path is None:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as stream:
            stream.write(DEFAULT)
    except FileExistsError:
        pass
    return path


def validate(template, source):
    if "\0" in template:
        raise Error(f"提示词模板不能包含 NUL 字符：{source}")
    if "{{issue_id}}" not in template:
        raise Error(f"提示词模板必须包含 {{{{issue_id}}}}：{source}")


def edit(project):
    editor = shlex.split(os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi")
    if not editor:
        raise Error("编辑器配置为空")
    require(editor[0])
    path = initialize(project["workspace"], project["project_id"])
    print(f"项目：{clean(project['name'])} ({clean(project['workspace_slug'])})", flush=True)
    print(f"提示词：{path}", flush=True)
    if subprocess.run([*editor, str(path)]).returncode:
        raise Error("编辑器退出失败")
    validate(path.read_text(encoding="utf-8"), path)
    return path


def render(identifier, workspace, workspace_id, project_id):
    path = prompt_path(workspace_id, project_id)
    template = path.read_text(encoding="utf-8") if path is not None and path.exists() else DEFAULT
    validate(template, path or "内置默认模板")
    result = template.replace("{{issue_id}}", identifier)
    # Prefix also prevents CLI prompt text beginning with '-' or '@' from becoming an option/file.
    return (
        f"Linear workspace: {workspace} (organization ID: {workspace_id})\n"
        f"All Linear queries must explicitly use --workspace {shlex.quote(workspace)}.\n\n"
        + result
    )
