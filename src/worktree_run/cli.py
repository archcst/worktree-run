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

from . import __version__, agents, bindings, config, prompt, runner, worktree
from .linear import Linear
from .store import Store
from .tmux import Tmux
from .util import Error, clean, require

COMMANDS = {"start", "project", "binding", "agent", "config", "prompt", "runs", "_run"}


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
    matches = [
        r
        for r in rows
        if answer in (r.get("name"), r.get("project_id"), r.get("target_id"), r.get("id"))
    ]
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
                "--preview-window=right,50%,wrap",
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
    repo = worktree.main_repository(
        repo or getattr(args, "repo", None) or ask("main worktree 绝对路径", previous.get("repo")),
        command="wtr project add",
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
        matches = [r for r in rows if name == r.get("project_id")]
        if not matches:
            matches = [r for r in rows if name == r.get("name")]
        if not matches:
            targets = [r for r in rows if name == r.get("target_id")]
            matches = [r for r in targets if r.get("enabled")] or targets
        if len(matches) != 1:
            raise Error("记录不存在或名称重复；请使用明确 ID")
        return matches[0]
    return choose(
        rows,
        lambda r: (
            bindings.describe(r)
            if "kind" in r
            else f"{r.get('name')} {r.get('workspace_slug', '')} {r.get('project_id', '')}"
        ),
    )


def remote_target(linear, kind, name=None):
    rows = linear.projects() if kind == "project" else linear.labels()
    if name:
        matches = [row for row in rows if name in (row["id"], row["name"])]
        if len(matches) != 1:
            details = "; ".join(bindings.target_text(kind, row) for row in matches)
            raise Error(f"Linear {bindings.KINDS[kind]} 不存在或重名，请使用明确 ID。{details}")
        return matches[0]
    return choose(rows, lambda row: bindings.target_text(kind, row))


def binding_command(store, args):
    repo = worktree.main_repository(Path.cwd()) if args.action == "add" else None
    rows = store.projects(args.workspace)
    if args.action == "list":
        output(rows)
        return
    if args.action == "add":
        if args.project is not None:
            kind, name = "project", args.project
        elif args.label is not None:
            kind, name = "label", args.label
        else:
            kind = choose(
                [dict(id=k, name=v) for k, v in bindings.KINDS.items()], lambda r: r["name"]
            )["id"]
            name = None
        linear = Linear(args.workspace)
        target = remote_target(linear, kind, name)
        values = {"name": args.new_name} if args.new_name is not None else {}
        row = bindings.from_target(linear, kind, target, values)
        if store.binding(*bindings.key(row)):
            raise Error("该对象已匹配其他 WTR 项目，请使用 wtr project edit")
    else:
        rows = [r for r in rows if not args.kind or r["kind"] == args.kind]
        row = select_local(rows, args.name)
    if args.action == "remove":
        if row["enabled"]:
            store.remove_binding(*bindings.key(row))
        return
    previous = dict(row)
    changing_target = args.action == "edit" and (args.project is not None or args.label is not None)
    if changing_target:
        kind, name = (
            ("project", args.project) if args.project is not None else ("label", args.label)
        )
        linear = Linear(row["workspace_slug"])
        if linear.workspace != row["workspace"]:
            raise Error("Linear workspace 与原绑定不一致，未修改绑定")
        target = remote_target(linear, kind, name)
        row = bindings.from_target(linear, kind, target, dict(row, enabled=1))
        if args.repo or args.baseline or args.root:
            values = argparse.Namespace(
                **{
                    field: getattr(args, field) or row[field]
                    for field in ("repo", "baseline", "root")
                }
            )
            row.update(binding_values(values))
    elif not (
        args.action == "edit"
        and args.new_name is not None
        and not (args.repo or args.baseline or args.root)
    ):
        row.update(binding_values(args, row, repo=repo))
    if args.new_name is not None:
        if not args.new_name.strip():
            raise Error("WTR 项目名称不能为空")
        row["name"] = args.new_name.strip()
    if args.action == "edit":
        store.replace_binding(previous, row)
    else:
        store.save_binding(row)
    output(row)


def prompt_project(store, args):
    rows = store.projects(args.workspace)
    if not rows:
        raise Error("没有本地 WTR 项目；请先运行 wtr project add 或在 wtr config 的项目管理中创建")
    if args.project:
        matches = [r for r in rows if args.project in (r["name"], r["project_id"])]
        if len(matches) != 1:
            raise Error(
                "本地 WTR 项目不存在或名称重复；请使用本地项目 ID，或运行 wtr project add 创建"
            )
        return matches[0]

    cwd = Path.cwd().resolve()
    by_id = {(row["workspace"], row["project_id"]): row for row in rows}
    locations = [(Path(row["repo"]).resolve(), row) for row in rows]
    for issue in store.all("SELECT * FROM issues"):
        if args.workspace and issue["workspace_slug"] != args.workspace:
            continue
        locations.append(
            (
                Path(issue["worktree"]).resolve(),
                by_id.get((issue["workspace"], issue["wtr_project_id"])),
            )
        )
    matches = [(len(path.parts), row) for path, row in locations if cwd.is_relative_to(path)]
    if matches:
        depth = max(length for length, _ in matches)
        candidates = [row for length, row in matches if length == depth]
        if any(row is None for row in candidates):
            raise Error(
                "当前 issue 没有可用的本地 WTR 项目；请创建 WTR 项目或显式指定 wtr prompt edit <本地项目名称或ID>"
            )
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
        project = store.project(linear.workspace, previous.get("wtr_project_id"))
        if project is None:
            raise Error(
                "已有 issue 关联的本地 WTR 项目不存在；请恢复该项目配置，不能改用远端项目模板"
            )
        return project
    matches = bindings.matching(store, linear.workspace, issue)
    if len(matches) == 1:
        return matches[0]
    if matches:
        if dry_run or not sys.stdin.isatty():
            details = "\n".join(bindings.describe(row) for row in matches)
            raise Error(f"issue 命中多个仓库绑定，请在交互终端明确选择：\n{details}")
        print("issue 命中多个仓库绑定，请选择本次使用的一项：")
        return choose(matches, bindings.describe)
    project = issue.get("project")
    labels = issue["labels"]["nodes"]
    details = (
        f"Project: {project['name'] if project else '无'}；"
        f"Labels: {', '.join(label['name'] for label in labels) or '无'}"
    )
    message = f"issue 未匹配仓库绑定（{details}）；请运行 wtr config 的「项目管理」创建 WTR 项目并配置匹配条件"
    if dry_run or not sys.stdin.isatty():
        raise Error(message)
    candidates = []
    if project:
        candidates.append(bindings.from_target(linear, "project", project, {}))
    candidates.extend(bindings.from_target(linear, "label", label, {}) for label in labels)
    if not candidates:
        raise Error(message + "；请先为 issue 指定 Project 或 Label")
    print(clean(message) + "，或选择下列对象立即创建绑定：")
    row = choose(candidates, bindings.describe)
    row.update(binding_values(argparse.Namespace(repo=None, baseline=None, root=None)))
    store.save_binding(row)
    return row


def selected_agent(store, override, project=None):
    name = (
        override if override is not None else (project or {}).get("agent") or store.setting("agent")
    )
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
        name = selected_agent(store, args.agent, binding)
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
            name = selected_agent(store, args.agent, binding)
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
    group = subs.add_parser(
        "project", aliases=["binding"], help="管理本地 WTR 项目及 Linear 匹配条件"
    ).add_subparsers(dest="action", required=True)
    for action in ("add", "list", "edit", "remove"):
        item = group.add_parser(
            action,
            description="在 Git main worktree 根目录执行，自动绑定当前目录。"
            if action == "add"
            else None,
        )
        if action == "list":
            continue
        if action != "add":
            item.add_argument("name", nargs="?", help="本地项目名称、项目 ID 或匹配目标 ID")
            item.add_argument("--kind", choices=bindings.KINDS, help="筛选当前绑定类型")
        if action in ("add", "edit"):
            item.add_argument("--name", dest="new_name", help="WTR 本地项目名称")
            target = item.add_mutually_exclusive_group()
            target.add_argument("--project", help="目标 Project 的完整名称或 ID")
            target.add_argument("--label", help="目标 Label 的完整名称或 ID")
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
        help="打开交互配置菜单（项目管理、项目提示词、默认 agent、tmux）",
        description="不带子命令时打开交互配置菜单；list/set 用于脚本和快捷操作。",
    ).add_subparsers(dest="action")
    group.add_parser("list")
    setting = group.add_parser("set")
    setting.add_argument("key", choices=["agent", "tmux-session"])
    setting.add_argument("value")
    prompt_p = subs.add_parser("prompt", help="编辑项目专属提示词")
    prompt_p.add_argument("action", choices=["edit"])
    prompt_p.add_argument(
        "project", nargs="?", help="项目名称或ID；默认按当前目录识别，否则选择项目"
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
        readonly = (
            getattr(args, "dry_run", False)
            or args.command == "agent"
            or (args.command in ("project", "binding", "config") and args.action == "list")
        )
        store = Store(args.db, readonly=readonly)
        if args.command == "start":
            start(store, args)
        elif args.command == "_run":
            return runner.internal_run(store, args.run_id)
        elif args.command == "agent":
            output(agents.catalog(store.setting("agent")))
        elif args.command in ("project", "binding"):
            if args.action == "list":
                binding_command(store, args)
            else:
                with store.lock():
                    binding_command(store, args)
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
