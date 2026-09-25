"""argparse commands and interactive configuration."""

import argparse
import json
import os
import shlex
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

from . import __version__, agents, config, prompt, runner, worktree
from .linear import Linear
from .store import Store
from .tmux import Tmux
from .util import Error, clean, require

COMMANDS = {"start", "project", "agent", "config", "prompt", "runs", "_run"}


def ask(label, default=None, *, editable=False):
    if not sys.stdin.isatty():
        raise Error(f"缺少配置：{label}；请在交互终端配置，或通过命令参数提供")
    if editable and default is not None:
        from prompt_toolkit import prompt as input_prompt
        from prompt_toolkit.completion import PathCompleter

        value = input_prompt(
            f"{label}（可编辑，回车确认）: ",
            default=default,
            completer=PathCompleter(only_directories=True, expanduser=True),
            complete_while_typing=False,
        ).strip()
    else:
        value = input(f"{label}" + (f" [{default}]" if default is not None else "") + ": ").strip()
    value = value or default
    if not value:
        raise Error(f"{label} 不能为空")
    return value


def choose(rows, label):
    if not rows:
        raise Error("没有可选记录")
    if not sys.stdin.isatty():
        raise Error("选择需要交互终端；请显式指定 ID/名称")
    for index, row in enumerate(rows, 1):
        print(f"{index}. {clean(label(row))}")
    answer = ask("输入序号或完整名称/ID")
    if answer.isdigit() and 1 <= int(answer) <= len(rows):
        return rows[int(answer) - 1]
    matches = [r for r in rows if answer in (r.get("name"), r.get("project_id"), r.get("id"))]
    if len(matches) != 1:
        raise Error("选择不唯一或不存在，请使用序号/ID")
    return matches[0]


def select_issue(issues):
    if not issues:
        raise Error("没有分配给你的未完成 issue")
    require("fzf")
    preview_code = (
        "import json,sys; d=json.load(open(sys.argv[1],encoding='utf-8')); "
        "i=d[int(sys.argv[2])]; print(i)"
    )
    with tempfile.TemporaryDirectory(prefix="wtr-preview-") as directory:
        preview = Path(directory) / "issues.json"
        descriptions = []
        lines = []
        for index, issue in enumerate(issues):
            descriptions.append(
                "\n".join(
                    clean(line) for line in (issue.get("description") or "（无描述）").splitlines()
                )
            )
            fields = [
                issue["identifier"],
                issue["title"],
                (issue.get("project") or {}).get("name", "（无项目）"),
                issue["state"]["name"],
                "P" + str(issue.get("priority") or "无"),
            ]
            lines.append(str(index) + "\t" + "\t".join(clean(f) for f in fields))
        preview.write_text(json.dumps(descriptions, ensure_ascii=False), encoding="utf-8")
        command = shlex.join([sys.executable, "-c", preview_code, str(preview)]) + " {1}"
        env = os.environ.copy()
        # User fzf defaults may add multi-select or execute bindings; keep this picker deterministic.
        env.pop("FZF_DEFAULT_OPTS", None)
        env.pop("FZF_DEFAULT_OPTS_FILE", None)
        result = subprocess.run(
            [
                "fzf",
                "--no-multi",
                "--delimiter=\t",
                "--with-nth=2..",
                "--prompt=Linear issue > ",
                "--preview",
                command,
            ],
            input="\n".join(lines),
            text=True,
            stdout=subprocess.PIPE,
            env=env,
        )
        if result.returncode in (1, 130):
            return None
        if result.returncode:
            raise Error(f"fzf 失败 ({result.returncode})")
        try:
            index = int(result.stdout.split("\t", 1)[0])
            if index < 0:
                raise ValueError()
            return issues[index]
        except (ValueError, IndexError) as exc:
            raise Error("fzf 返回了无效的选择") from exc


def binding_values(args, previous=None, *, repo=None):
    previous = previous or {}
    repo = worktree.repository(
        repo or getattr(args, "repo", None) or ask("main worktree 绝对路径", previous.get("repo"))
    )
    baseline = args.baseline or ask("新分支基线", previous.get("baseline", "origin/main"))
    worktree.verify_baseline(repo, baseline)
    root = str(
        Path(
            args.root
            or ask(
                "issue worktree 根目录",
                previous.get("root", repo),
                editable=True,
            )
        )
        .expanduser()
        .resolve()
    )
    return dict(repo=repo, baseline=baseline, root=root)


def select_local(rows, name):
    if name:
        matches = [r for r in rows if name in (r.get("name"), r.get("project_id"))]
        if len(matches) != 1:
            raise Error("记录不存在或名称重复；请使用明确 ID")
        return matches[0]
    return choose(
        rows, lambda r: f"{r.get('name')} {r.get('workspace_slug', '')} {r.get('project_id', '')}"
    )


def project_command(store, args):
    repo = worktree.main_repository(Path.cwd()) if args.action == "add" else None
    rows = store.all("SELECT * FROM projects ORDER BY workspace_slug,name")
    if args.workspace:
        rows = [r for r in rows if r["workspace_slug"] == args.workspace]
    if args.action == "list":
        output(rows)
        return
    if args.action == "add":
        linear = Linear(args.workspace)
        projects = linear.projects()
        if args.name:
            matches = [p for p in projects if args.name in (p["id"], p["name"])]
            if len(matches) != 1:
                raise Error("Linear 项目不存在或重名，请使用 project ID")
            project = matches[0]
        else:
            project = choose(projects, lambda p: f"{p['name']} [{p['id']}]")
        if store.project(linear.workspace, project["id"]):
            raise Error("项目已绑定，请使用 wtr project edit")
        row = dict(
            workspace=linear.workspace,
            workspace_slug=linear.slug,
            project_id=project["id"],
            name=project["name"],
        )
    else:
        row = select_local(rows, args.name)
    if args.action == "remove":
        store.write(
            "DELETE FROM projects WHERE workspace=? AND project_id=?",
            (row["workspace"], row["project_id"]),
        )
        return
    row.update(binding_values(args, row, repo=repo))
    prompt.initialize(row["workspace"], row["project_id"])
    store.save_project(row)
    output(row)


def prompt_project(store, args):
    rows = store.all("SELECT * FROM projects ORDER BY workspace_slug,name")
    if args.workspace:
        rows = [row for row in rows if row["workspace_slug"] == args.workspace]
    if not rows:
        raise Error("尚未绑定项目；请先在 main worktree 根目录运行 wtr project add")
    if args.project:
        return select_local(rows, args.project)

    cwd = Path.cwd().resolve()
    by_id = {(row["workspace"], row["project_id"]): row for row in rows}
    locations = [(Path(row["repo"]).resolve(), row) for row in rows]
    for issue in store.all("SELECT * FROM issues"):
        if args.workspace and issue["workspace_slug"] != args.workspace:
            continue
        locations.append(
            (
                Path(issue["worktree"]).resolve(),
                by_id.get((issue["workspace"], issue["project_id"])),
            )
        )
    matches = [(len(path.parts), row) for path, row in locations if cwd.is_relative_to(path)]
    if matches:
        depth = max(length for length, _ in matches)
        candidates = [row for length, row in matches if length == depth]
        if any(row is None for row in candidates):
            raise Error("当前 issue 没有已绑定的项目；请显式指定 wtr prompt edit <项目名称或ID>")
        rows = list({(row["workspace"], row["project_id"]): row for row in candidates}.values())
        if len(rows) == 1:
            return rows[0]
    return select_local(rows, None)


def edit_prompt(store, args):
    prompt.edit(prompt_project(store, args))


def output(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def resolve_binding(store, linear, issue, dry_run):
    previous = store.issue(linear.workspace, issue["id"])
    if previous:
        return previous
    project = issue.get("project")
    binding = store.project(linear.workspace, project["id"]) if project else None
    if binding:
        return binding
    if dry_run:
        raise Error("issue 无关联且项目未绑定；请先运行 wtr project add 或在交互模式选择仓库")
    print("该 issue 没有项目绑定，请选择本次使用的仓库。")
    binding = binding_values(argparse.Namespace(repo=None, baseline=None, root=None))
    if project and ask("保存为该项目的持久绑定？y/N", "N").lower() == "y":
        store.save_project(
            dict(
                binding,
                workspace=linear.workspace,
                workspace_slug=linear.slug,
                project_id=project["id"],
                name=project["name"],
            )
        )
    return binding


def selected_agent(store, override):
    name = store.setting("agent") if override is None else override
    if name:
        agents.get(name)
        return name
    rows = [row for row in agents.catalog() if row["installed"]]
    if not rows:
        raise Error(
            "没有已安装的内置 agent；运行 wtr agent list 查看支持项，并先安装和认证其中一个"
        )
    if not sys.stdin.isatty():
        raise Error("请选择 agent：使用 --agent <名称>，或 wtr config set agent <名称> 设置默认值")
    print("选择本次使用的 agent（可用 wtr config set agent <名称> 设置默认值）：")
    return choose(rows, lambda row: f"{row['name']} — {row['label']} ({row['executable']})")["name"]


def start(store, args):
    linear = Linear(args.workspace)
    if args.issue:
        issue = linear.issue(args.issue)
    else:
        issue = select_issue(linear.mine())
        if issue is None:
            return
        issue = linear.issue(issue["id"])
        if not linear.is_mine_open(issue):
            raise Error("issue 分配或状态已变化，请重新选择")
    if args.dry_run:
        active = store.active(linear.workspace, issue["id"])
        if active:
            pane = Tmux(active["tmux_socket"]).locate(store, active)
            if pane and pane["dead"] == "0":
                output({"action": "focus", "run": active["id"], "pane": pane})
                return
        binding = resolve_binding(store, linear, issue, True)
        name = selected_agent(store, args.agent)
        row, action, argv, session = runner.plan(store, linear, issue, binding, name)
        if action == "create":
            worktree.verify_baseline(row["repo"], row["baseline"])
        output(
            {
                "action": action,
                "issue": row,
                "agent": name,
                "argv": argv,
                "tmux_session": session,
                "note": "只读预览；远端基线创建时才 fetch，宿主异步环境准备不等待",
            }
        )
        return
    with store.lock():
        runner.recover(store, linear.workspace, issue["id"])
        run = store.active(linear.workspace, issue["id"])
        if not run:
            binding = resolve_binding(store, linear, issue, False)
            name = selected_agent(store, args.agent)
            run = runner.launch(store, linear, issue, binding, name)
    if args.no_attach:
        output(
            {
                key: run[key]
                for key in ("id", "status", "session_id", "window_id", "pane_id", "tmux_socket")
            }
        )
    else:
        Tmux(run["tmux_socket"]).focus(run)


def parser():
    root = argparse.ArgumentParser(
        prog="wtr", description="选择 Linear issue，在独立 worktree / tmux window 启动 coding agent"
    )
    root.add_argument("--version", action="version", version=__version__)
    root.add_argument("--db", help="SQLite 路径（默认使用 XDG_DATA_HOME）")
    root.add_argument("--workspace", help="Linear workspace slug；默认复用 Linear CLI 当前配置")
    subs = root.add_subparsers(dest="command", required=True)
    start_p = subs.add_parser("start", help="启动 issue（可省略 start）")
    start_p.add_argument("issue", nargs="?")
    start_p.add_argument(
        "--agent",
        nargs="?",
        const="",
        metavar="NAME",
        help="本次使用的内置 agent；不带名称时交互选择（优先于默认值）",
    )
    start_p.add_argument("--dry-run", action="store_true")
    start_p.add_argument("--no-attach", action="store_true", help="仅创建后台窗口")
    group = subs.add_parser("project").add_subparsers(dest="action", required=True)
    for action in ("add", "list", "edit", "remove"):
        item = group.add_parser(
            action,
            description="在 Git main worktree 根目录执行，自动绑定当前目录。"
            if action == "add"
            else None,
        )
        if action == "list":
            continue
        item.add_argument("name", nargs="?", help="名称或 project ID")
        if action in ("add", "edit"):
            if action == "edit":
                item.add_argument("--repo")
            item.add_argument("--baseline")
            item.add_argument("--root")
    subs.add_parser("agent", help="查看内置 agent 和安装状态").add_argument(
        "action", nargs="?", choices=["list"], default="list"
    )
    group = subs.add_parser(
        "config",
        help="打开交互配置菜单（项目、提示词、默认 agent、tmux）",
        description="不带子命令时打开交互配置菜单；list/set 用于脚本和快捷操作。",
    ).add_subparsers(dest="action")
    group.add_parser("list")
    setting = group.add_parser("set")
    setting.add_argument("key", choices=["agent", "tmux-session"])
    setting.add_argument("value")
    prompt_p = subs.add_parser("prompt", help="编辑项目专属提示词")
    prompt_p.add_argument("action", choices=["edit"])
    prompt_p.add_argument(
        "project", nargs="?", help="绑定项目的名称或ID；默认按当前目录识别，否则选择项目"
    )
    subs.add_parser("runs", help="查看历史，并恢复遗留执行状态")
    internal = subs.add_parser("_run", help="内部执行入口（仅由 tmux 调用）")
    internal.add_argument("run_id")
    return root


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    # Global options are accepted before or after an implicit issue/subcommand.
    globals_p = argparse.ArgumentParser(add_help=False)
    globals_p.add_argument("--db")
    globals_p.add_argument("--workspace")
    global_args, remaining = globals_p.parse_known_args(argv)
    if not remaining or (
        remaining[0] not in COMMANDS and remaining[0] not in ("-h", "--help", "--version")
    ):
        remaining.insert(0, "start")
    args = parser().parse_args(remaining)
    args.db, args.workspace = global_args.db, global_args.workspace
    store = None
    try:
        store = Store(args.db, readonly=getattr(args, "dry_run", False))
        if args.command == "start":
            start(store, args)
        elif args.command == "_run":
            return runner.internal_run(store, args.run_id)
        elif args.command == "agent":
            output(agents.catalog(store.setting("agent")))
        elif args.command == "project":
            with store.lock():
                project_command(store, args)
        elif args.command == "config":
            if args.action is None:
                from .config_ui import run as configure

                configure(store, args.workspace)
            elif args.action == "list":
                output(
                    {
                        "agent": store.setting("agent"),
                        "tmux-session": store.setting("tmux-session", "worktree-run"),
                    }
                )
            else:
                with store.lock():
                    config.set_value(store, args.key, args.value)
        elif args.command == "prompt":
            edit_prompt(store, args)
        elif args.command == "runs":
            with store.lock():
                runner.recover(store)
            output(
                store.all(
                    "SELECT id,identifier,workspace_slug,agent,status,created_at,ended_at,exit_code,error,session_id,window_id,pane_id FROM runs ORDER BY created_at DESC"
                )
            )
        return 0
    except (Error, OSError, sqlite3.Error, ValueError) as exc:
        print(f"wtr: {clean(exc)}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\nwtr: 已取消", file=sys.stderr)
        return 130
    finally:
        if store:
            store.close()
