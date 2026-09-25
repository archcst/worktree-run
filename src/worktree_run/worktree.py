"""Non-destructive Git worktree preparation."""

import hashlib
import os
from pathlib import Path

from .util import Error, execute, require


def git(repo, *args, check=True):
    return execute(["git", "-C", repo, *args], check=check)


def repository(path):
    require("git")
    repo = Path(path).expanduser().resolve()
    top = git(repo, "rev-parse", "--show-toplevel").stdout.strip()
    if Path(top).resolve() != repo:
        raise Error(f"请指定 main worktree 根目录：{top}")
    return str(repo)


def common_dir(repo):
    return Path(
        git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
    ).resolve()


def worktrees(repo):
    output = git(repo, "worktree", "list", "--porcelain", "-z").stdout
    trees = []
    for record in output.split("\0\0"):
        fields = {}
        for line in record.split("\0"):
            if line:
                key, _, value = line.partition(" ")
                fields[key] = value
        if "worktree" in fields:
            trees.append(fields)
    return trees


def main_repository(path, *, command="wtr project add"):
    """Require the root of Git's primary (not linked) worktree, on any branch."""
    require("git")
    repo = Path(path).expanduser().resolve()
    top = git(repo, "rev-parse", "--show-toplevel", check=False)
    if top.returncode:
        raise Error(f"{command} 必须在 Git main worktree 的根目录执行")
    root = Path(top.stdout.strip()).resolve()
    git_dir = Path(git(repo, "rev-parse", "--absolute-git-dir").stdout.strip()).resolve()
    if git_dir != common_dir(repo):
        trees = worktrees(repo)
        if not trees or "bare" in trees[0]:
            raise Error("该仓库没有 main worktree；请在普通 Git 仓库的主工作区根目录执行")
        main = Path(trees[0]["worktree"]).resolve()
        hint = git(main, "rev-parse", "--show-toplevel", check=False)
        suffix = (
            f"：{main}"
            if not hint.returncode and Path(hint.stdout.strip()).resolve() == main
            else ""
        )
        raise Error(
            f"当前目录属于附加 worktree，请进入 main worktree 根目录后运行 {command}{suffix}"
        )
    if repo != root:
        raise Error(f"请进入 main worktree 根目录后运行 {command}：{root}")
    return str(repo)


def baseline_remote(repo, baseline):
    if not baseline or baseline.startswith("-") or "\0" in baseline:
        raise Error("无效的分支基线")
    short = baseline.removeprefix("refs/remotes/")
    remotes = git(repo, "remote").stdout.splitlines()
    for remote in sorted(remotes, key=len, reverse=True):
        if short.startswith(remote + "/"):
            branch = short[len(remote) + 1 :]
            if branch == "HEAD":
                target = git(repo, "symbolic-ref", "--quiet", "refs/remotes/" + short, check=False)
                if target.returncode:
                    raise Error("远端 HEAD 不可解析，请配置显式基线（例如 origin/main）")
                branch = target.stdout.strip().removeprefix("refs/remotes/" + remote + "/")
            if git(repo, "check-ref-format", "refs/heads/" + branch, check=False).returncode:
                raise Error("远端基线必须是分支引用，例如 origin/main")
            return remote, branch
    if baseline.startswith("refs/remotes/"):
        raise Error(f"远端不存在：{baseline}")
    return None


def verify_baseline(repo, baseline):
    baseline_remote(repo, baseline)
    result = git(
        repo, "rev-parse", "--verify", "--end-of-options", baseline + "^{commit}", check=False
    )
    if result.returncode:
        raise Error(f"基线不可解析：{baseline}（请检查绑定和远端引用）")
    return result.stdout.strip()


def route(store, linear, issue, binding):
    previous = store.issue(linear.workspace, issue["id"])
    if previous:
        repository(previous["repo"])
        previous.update(
            identifier=issue["identifier"],
            workspace_slug=linear.slug,
            project_id=(issue.get("project") or {}).get("id"),
        )
        return previous
    repo = repository(binding["repo"])
    root = Path(binding["root"]).expanduser().resolve()
    slug = issue["identifier"].lower()
    branch = "issue/" + slug
    path = root / slug
    # Known collisions across workspaces get deterministic disambiguation.
    collisions = store.all("SELECT * FROM issues WHERE worktree=? OR branch=?", (str(path), branch))
    if any(
        r["worktree"] == str(path)
        or (Path(r["repo"]).is_dir() and common_dir(r["repo"]) == common_dir(repo))
        for r in collisions
    ):
        suffix = hashlib.sha256((linear.workspace + ":" + issue["id"]).encode()).hexdigest()[:10]
        branch += "-" + suffix
        path = root / (slug + "-" + suffix)
    return dict(
        workspace=linear.workspace,
        issue_id=issue["id"],
        identifier=issue["identifier"],
        workspace_slug=linear.slug,
        project_id=(issue.get("project") or {}).get("id"),
        repo=repo,
        baseline=binding["baseline"],
        branch=branch,
        worktree=str(path),
    )


def inspect(row, *, associated):
    repo, path, branch = row["repo"], Path(row["worktree"]), row["branch"]
    repository(repo)
    if path.is_symlink():
        raise Error(f"worktree 路径是符号链接，拒绝使用：{path}")
    expected = "refs/heads/" + branch
    trees = worktrees(repo)
    at_path = next((t for t in trees if Path(t["worktree"]).resolve() == path.resolve()), None)
    if at_path:
        if not associated:
            raise Error(f"目标 worktree 已存在且未关联，拒绝接管：{path}")
        if at_path.get("branch") != expected or "prunable" in at_path or not path.is_dir():
            raise Error(f"已关联 worktree 的分支或路径不一致：{path}")
        if common_dir(path) != common_dir(repo):
            raise Error(f"worktree 不属于绑定仓库：{path}")
        return "reuse"
    if any(t.get("branch") == expected for t in trees):
        raise Error(f"分支已在其他 worktree 检出：{branch}")
    if os.path.lexists(path):
        raise Error(f"目标路径已占用，未修改任何内容：{path}")
    if git(repo, "show-ref", "--verify", "--quiet", expected, check=False).returncode == 0:
        raise Error(f"分支已存在但没有可复用关联：{branch}；请手动解决冲突")
    if git(repo, "check-ref-format", "--branch", branch, check=False).returncode:
        raise Error(f"无效分支名称：{branch}")
    baseline_remote(repo, row["baseline"])
    return "create"


def prepare(row, *, associated):
    if inspect(row, associated=associated) == "reuse":
        return
    repo, baseline = row["repo"], row["baseline"]
    remote = baseline_remote(repo, baseline)
    if remote:
        name, branch = remote
        git(repo, "fetch", "--", name, f"+refs/heads/{branch}:refs/remotes/{name}/{branch}")
    commit = verify_baseline(repo, baseline)
    Path(row["worktree"]).parent.mkdir(parents=True, exist_ok=True)
    git(repo, "worktree", "add", "-b", row["branch"], "--", row["worktree"], commit)
    inspect(row, associated=True)
