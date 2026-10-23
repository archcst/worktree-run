"""Interactive configuration with short locks and stable binding identities."""

import sqlite3
import sys
from pathlib import Path

from . import agents, bindings, config, prompt, worktree
from .linear import Linear
from .util import Error, clean


def menu(title, choices, *, text="", default=None, search=False):
    """Menu with Emacs/Vim navigation and a separate search-input mode."""
    from prompt_toolkit.application import Application
    from prompt_toolkit.filters import Condition
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.keys import Keys
    from prompt_toolkit.layout import HSplit, Layout, Window
    from prompt_toolkit.layout.controls import FormattedTextControl
    from prompt_toolkit.layout.margins import ScrollbarMargin
    from prompt_toolkit.styles import Style

    if not choices:
        return None
    choices = [(value, clean(label)) for value, label in choices]
    query = ""
    editing_search = False
    navigation = Condition(lambda: not editing_search)
    index = next(
        (i for i, (value, _) in enumerate(choices) if default is not None and value == default), 0
    )
    keys = KeyBindings()

    def visible():
        return [
            (value, label)
            for value, label in choices
            if value is None or query.casefold() in label.casefold()
        ]

    @keys.add("up")
    @keys.add("c-p")
    @keys.add("k", filter=navigation)
    def up(event):
        nonlocal index
        index = max(0, index - 1)

    @keys.add("down")
    @keys.add("c-n")
    @keys.add("j", filter=navigation)
    def down(event):
        nonlocal index
        index = min(max(0, len(visible()) - 1), index + 1)

    @keys.add("home")
    @keys.add("escape", "<")
    @keys.add("g", "g", filter=navigation)
    def home(event):
        nonlocal index
        index = 0

    @keys.add("end")
    @keys.add("escape", ">")
    @keys.add("G", filter=navigation)
    def end(event):
        nonlocal index
        index = max(0, len(visible()) - 1)

    @keys.add("enter")
    def accept(event):
        rows = visible()
        if rows:
            event.app.exit(result=rows[index][0])

    @keys.add("escape")
    @keys.add("c-g")
    def back(event):
        nonlocal editing_search
        if editing_search:
            editing_search = False
        else:
            event.app.exit(result=None)

    @keys.add("c-c")
    @keys.add("c-d")
    @keys.add("q", filter=navigation)
    def cancel(event):
        event.app.exit(result=None)

    if search:

        @keys.add("/", filter=navigation)
        def start_search(event):
            nonlocal editing_search
            editing_search = True

        @keys.add(Keys.Any)
        def type_query(event):
            nonlocal query, index, editing_search
            if event.data.isprintable():
                editing_search = True
                query += event.data
                index = 0

        @keys.add("backspace")
        def backspace(event):
            nonlocal query, index, editing_search
            editing_search = True
            query = query[:-1]
            index = 0

        @keys.add("c-u")
        def clear(event):
            nonlocal query, index
            query, index = "", 0
    else:
        for number in range(1, min(len(choices), 9) + 1):

            @keys.add(str(number))
            def select_number(event, selected=number - 1):
                nonlocal index
                index = selected

    def header():
        fragments = [("class:title", clean(title) + "\n")]
        if text:
            fragments.append(("", "\n".join(clean(line) for line in text.splitlines()) + "\n"))
        if search:
            mode = "搜索输入中 · Esc 返回导航" if editing_search else "导航模式 · / 搜索"
            fragments.append(("class:hint", f"搜索：{query}（{mode}）\n"))
        return fragments

    def items():
        fragments = []
        for i, (_, label) in enumerate(visible()):
            if i == index:
                fragments.append(("[SetCursorPosition]", ""))
            number = f"{i + 1}. " if not search else ""
            fragments.append(
                (
                    "class:selected" if i == index else "",
                    ("› " if i == index else "  ") + number + label + "\n",
                )
            )
        return fragments or [("class:hint", "没有匹配项")]

    control = FormattedTextControl(items, focusable=True)
    footer = "↑↓ / j k / Ctrl+n p 选择 · Enter 确认 · Esc/Ctrl+g 返回\n"
    footer += "gg/G 或 Alt+</> 首尾 · q 退出当前菜单"
    footer += " · / 搜索 · Ctrl+U 清空" if search else " · 数字定位"
    body = HSplit(
        [
            Window(FormattedTextControl(header), dont_extend_height=True, wrap_lines=True),
            Window(control, wrap_lines=True, right_margins=[ScrollbarMargin()]),
            Window(FormattedTextControl(footer), height=2),
        ],
        padding=1,
    )
    app = Application(
        layout=Layout(body, focused_element=control),
        key_bindings=keys,
        style=Style.from_dict({"title": "bold", "selected": "reverse", "hint": "italic"}),
        full_screen=True,
    )
    try:
        return app.run()
    except (KeyboardInterrupt, EOFError):
        return None


def input_text(label, default="", *, path=False):
    from prompt_toolkit import prompt as input_prompt
    from prompt_toolkit.completion import PathCompleter
    from prompt_toolkit.key_binding import KeyBindings

    keys = KeyBindings()

    @keys.add("escape")
    def cancel(event):
        event.app.exit(exception=KeyboardInterrupt)

    try:
        return input_prompt(
            f"{label}（Enter 确认，Esc 取消）: ",
            default=default,
            completer=PathCompleter(only_directories=True, expanduser=True) if path else None,
            complete_while_typing=False,
            key_bindings=keys,
        ).strip()
    except (KeyboardInterrupt, EOFError):
        return None


def notice(title, text):
    menu(title, [(None, "返回")], text=text)


def field(label, default, validate, *, path=False):
    while True:
        value = input_text(label, default, path=path)
        if value is None:
            return None
        try:
            return validate(value)
        except (Error, OSError, ValueError) as exc:
            notice("输入无效", str(exc))
            default = value


def binding_choices(rows):
    return [
        (
            (row["workspace"], row["project_id"]),
            bindings.describe(row)
            + f" · 目标 ID：{row['target_id']}"
            + (" · 匹配已停用" if not row["enabled"] else ""),
        )
        for row in rows
    ]


def project_choices(rows):
    return [
        (
            (row["workspace"], row["project_id"]),
            f"{row['name']} · {row['workspace_slug']} [{row['project_id']}]",
        )
        for row in rows
    ]


def edit_project_prompt(store, workspace):
    rows = store.projects(workspace)
    if not rows:
        notice("没有本地 WTR 项目", "请先在项目管理中创建 WTR 项目，或运行 wtr project add。")
        return
    choice = menu(
        "选择项目提示词",
        [*project_choices(rows), (None, "返回")],
        search=True,
    )
    if choice is None:
        return
    row = store.project(*choice)
    if row is None:
        raise Error("本地 WTR 项目已不存在，请重新选择")
    path = prompt.edit(row)
    notice("项目提示词已保存", str(path))


def valid_project_name(value):
    if not value.strip():
        raise Error("WTR 项目名称不能为空")
    return value.strip()


def valid_root(value):
    if not value:
        raise Error("issue worktree 根目录不能为空")
    path = Path(value).expanduser().resolve()
    if path.exists() and not path.is_dir():
        raise Error(f"不是目录：{path}")
    return str(path)


def valid_baseline(repo, value):
    worktree.verify_baseline(repo, value)
    return value


def suggested_baseline(repo):
    for value in ("origin/main", "origin/master", "main", "master", "HEAD"):
        try:
            worktree.verify_baseline(repo, value)
            return value
        except Error:
            continue
    return "origin/main"


def save_binding(store, row, *, expected=None):
    # Keep the lock out of menus, Linear queries, and editor sessions.
    with store.lock():
        current = store.project(row["workspace"], row["project_id"])
        if current != expected:
            raise Error("WTR 项目已被其他进程修改，请重新选择后再保存")
        if expected is None:
            occupied = store.binding(*bindings.key(row))
            if occupied is not None:
                raise Error("该匹配对象已被其他 WTR 项目使用，请重新选择")
            store.save_binding(row)
        else:
            store.replace_binding(expected, row)


def add_binding(store, workspace):
    repo = worktree.main_repository(Path.cwd(), command="wtr config")
    kind = menu(
        "选择绑定类型",
        [("project", "Project（项目）"), ("label", "Label（标签）"), (None, "返回")],
        text=f"仓库：{repo}",
    )
    if kind is None:
        return None
    linear = Linear(workspace)
    targets = linear.projects() if kind == "project" else linear.labels()
    rows = [row for row in targets if not store.binding(linear.workspace, kind, row["id"])]
    if not rows:
        notice("没有可绑定的对象", "当前 Linear 工作区没有该类型的未绑定对象，可返回管理已有绑定。")
        return None
    target = menu(
        "绑定当前仓库",
        [
            *[(row, bindings.target_text(kind, row)) for row in rows],
            (None, "返回"),
        ],
        text=f"仓库：{repo}\nLinear 工作区：{linear.slug}",
        search=True,
    )
    if target is None:
        return None
    baseline = field(
        "新分支基线", suggested_baseline(repo), lambda value: valid_baseline(repo, value)
    )
    if baseline is None:
        return None
    root = field("issue worktree 根目录", repo, valid_root, path=True)
    if root is None:
        return None
    row = bindings.from_target(linear, kind, target, dict(repo=repo, baseline=baseline, root=root))
    save_binding(store, row)
    return row["workspace"], row["project_id"]


def change_binding_target(store, row):
    kind = menu(
        "修改匹配方式",
        [("project", "Project（项目）"), ("label", "Label（标签）"), (None, "返回")],
        text=f"当前绑定：{bindings.describe(row)}",
        default=row["kind"],
    )
    if kind is None:
        return None
    linear = Linear(row["workspace_slug"])
    if linear.workspace != row["workspace"]:
        raise Error("Linear workspace 与原绑定不一致，未修改绑定")
    targets = linear.projects() if kind == "project" else linear.labels()
    target = menu(
        "选择匹配对象",
        [*[(target, bindings.target_text(kind, target)) for target in targets], (None, "返回")],
        text=f"Linear 工作区：{linear.slug}",
        default=next(
            (
                target
                for target in targets
                if kind == row["kind"] and target["id"] == row["target_id"]
            ),
            None,
        ),
        search=True,
    )
    if target is None:
        return None
    replacement = bindings.from_target(linear, kind, target, dict(row, enabled=1))
    confirmed = menu(
        "确认修改匹配方式/对象",
        [(False, "保留原绑定"), (True, "确认修改")],
        text=(
            f"原绑定：{bindings.describe(row)}\n新绑定：{bindings.describe(replacement)}\n"
            f"仓库：{row['repo']}\n基线：{row['baseline']}\nworktree 根目录：{row['root']}\n"
            "仓库设置、已有 issue 关联、项目提示词和执行记录保持不变。"
        ),
        default=False,
    )
    if confirmed is not True:
        return None
    with store.lock():
        store.replace_binding(row, replacement)
    return replacement["workspace"], replacement["project_id"]


def binding_settings(store, key):
    selected = "baseline"
    while True:
        row = store.project(*key)
        if row is None:
            notice("WTR 项目已不存在", "请返回项目列表重新选择。")
            return
        text = (
            f"绑定：{bindings.describe(row)}\nmain worktree：{row['repo']}\n"
            f"分支基线：{row['baseline']}\nissue worktree 根目录：{row['root']}\n"
            f"agent：{row['agent'] or '继承全局'}\ntmux session：{row['tmux_session'] or '继承全局'}\n"
            f"匹配：{'启用' if row['enabled'] else '停用'}\n修改后即时保存。"
        )
        action = menu(
            f"项目设置：{row['name']}",
            [
                ("target", "修改匹配方式/对象（启用匹配）"),
                ("baseline", "修改新分支基线"),
                ("root", "修改 issue worktree 根目录"),
                ("repo", "重新绑定到当前仓库"),
                ("remove", "停用匹配"),
                ("name", "重命名 WTR 项目"),
                ("prompt", "编辑项目提示词"),
                ("agent", "项目 agent 覆盖"),
                ("tmux", "项目 tmux session 覆盖"),
                (None, "返回项目列表"),
            ],
            text=text,
            default=selected,
        )
        if action is None:
            return
        selected = action
        try:
            if action == "target":
                replacement_key = change_binding_target(store, row)
                if replacement_key is not None:
                    key = replacement_key
            elif action in ("baseline", "root"):
                value = field(
                    "新分支基线" if action == "baseline" else "issue worktree 根目录",
                    row[action],
                    (lambda value: valid_baseline(row["repo"], value))
                    if action == "baseline"
                    else valid_root,
                    path=action == "root",
                )
                if value is not None:
                    save_binding(store, dict(row, **{action: value}), expected=row)
            elif action == "repo":
                repo = worktree.main_repository(Path.cwd(), command="wtr config")
                confirmed = menu(
                    "确认重新绑定",
                    [(False, "保留当前绑定"), (True, "确认绑定当前仓库")],
                    text=f"绑定：{bindings.describe(row)}\n当前绑定：{row['repo']}\n新仓库：{repo}\n已有 issue 的 worktree 关联不变。",
                    default=False,
                )
                if confirmed is True:
                    baseline = field(
                        "新分支基线", row["baseline"], lambda value: valid_baseline(repo, value)
                    )
                    if baseline is not None:
                        save_binding(store, dict(row, repo=repo, baseline=baseline), expected=row)
            elif action == "name":
                value = field("WTR 项目名称", row["name"], valid_project_name)
                if value is not None:
                    save_binding(store, dict(row, name=value), expected=row)
            elif action == "prompt":
                path = prompt.edit(row)
                notice("项目提示词已保存", str(path))
            elif action == "agent":
                choice = menu(
                    "项目 agent 覆盖",
                    [
                        ("inherit", "继承全局默认 agent"),
                        *[(agent["name"], agent["label"]) for agent in agents.catalog()],
                        (None, "返回"),
                    ],
                    default=row["agent"] or "inherit",
                )
                if choice is not None:
                    if choice != "inherit":
                        agents.get(choice)
                    save_binding(
                        store,
                        dict(row, agent=None if choice == "inherit" else choice),
                        expected=row,
                    )
            elif action == "tmux":
                value = field(
                    "项目 tmux session（留空继承全局）",
                    row["tmux_session"] or "",
                    lambda value: config.session_name(value) if value else "",
                )
                if value is not None:
                    save_binding(store, dict(row, tmux_session=value or None), expected=row)
            elif action == "remove":
                confirmed = menu(
                    "确认停用匹配",
                    [(False, "保留匹配"), (True, "确认停用匹配")],
                    text=f"项目：{bindings.describe(row)}\n仓库：{row['repo']}\n停用后保留本地项目、worktree、项目提示词和执行记录。",
                    default=False,
                )
                if confirmed is True:
                    with store.lock():
                        if store.project(*key) != row:
                            raise Error("WTR 项目已被其他进程修改，请重新选择后再操作")
                        if row["enabled"]:
                            store.remove_binding(*bindings.key(row))
                    return
        except (Error, OSError, ValueError, sqlite3.Error) as exc:
            notice("操作失败", str(exc))
        except (KeyboardInterrupt, EOFError):
            pass


def manage_bindings(store, workspace):
    while True:
        rows = store.projects(workspace)
        choice = menu(
            "项目管理",
            [
                ("add", "＋ 创建 WTR 项目（匹配 Project 或 Label）"),
                *binding_choices(rows),
                (None, "返回配置"),
            ],
            text=f"当前目录：{Path.cwd()}\n选择本地 WTR 项目进行编辑；新增项目需要位于 main worktree 根目录。",
            search=True,
        )
        if choice is None:
            return
        try:
            key = add_binding(store, workspace) if choice == "add" else choice
            if key is not None:
                binding_settings(store, key)
        except (Error, OSError, ValueError, sqlite3.Error) as exc:
            notice("操作失败", str(exc))


def set_default_agent(store):
    current = store.setting("agent")
    rows = agents.catalog(current)
    choice = menu(
        "默认 agent",
        [
            *[
                (
                    row["name"],
                    f"{row['label']} ({row['name']}) — "
                    + ("已安装" if row["installed"] else "未安装")
                    + (" · 当前默认" if row["default"] else ""),
                )
                for row in rows
            ],
            ("ask", "每次启动时选择 agent"),
            (None, "返回配置"),
        ],
        text="选择后保存为默认值；也可在启动任务时单次覆盖。",
        default=current or "ask",
    )
    if choice is None:
        return
    with store.lock():
        if choice == "ask":
            store.write("DELETE FROM settings WHERE key='agent'")
        else:
            config.set_value(store, "agent", choice)
    message = "启动新任务时选择 agent。" if choice == "ask" else f"默认 agent：{choice}"
    if choice != "ask" and not agents.get(choice).executable():
        message += "\n该 CLI 尚未安装或不在 PATH，请安装并认证后使用。"
    notice("默认 agent 已保存", message)


def set_tmux_session(store):
    value = field(
        "tmux session 名称", store.setting("tmux-session", "worktree-run"), config.session_name
    )
    if value is not None:
        with store.lock():
            config.set_value(store, "tmux-session", value)
        notice("tmux 设置已保存", f"新任务使用 session：{value}\n已有任务窗口保持不变。")


def select_workspace(store, current):
    slugs = sorted(
        {row["workspace_slug"] for row in store.projects()} | ({current} if current else set())
    )
    choice = menu(
        "工作区筛选",
        [
            (("scope", None), "全部工作区（新增使用 Linear CLI 默认工作区）"),
            *[(("scope", slug), slug) for slug in slugs],
            (("input", None), "输入其他工作区 slug"),
            (None, "返回配置"),
        ],
        text="只影响本次配置中的绑定和提示词项目范围，不修改 Linear CLI 的默认工作区。",
        default=("scope", current),
    )
    if choice is None:
        return current
    if choice[0] == "scope":
        return choice[1]
    value = input_text("Linear workspace slug（留空表示全部）", current or "")
    return current if value is None else value or None


def run(store, workspace=None):
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise Error("wtr config 需要交互终端；脚本可使用 wtr config list 或 wtr config set")
    selected = "bindings"
    while True:
        default = store.setting("agent") or "每次启动时选择"
        session = store.setting("tmux-session", "worktree-run")
        rows = store.projects(workspace)
        action = menu(
            "wtr 配置",
            [
                ("bindings", f"项目管理（{len(rows)} 个）"),
                ("prompt", "项目提示词"),
                ("agent", f"默认 agent：{default}"),
                ("tmux", f"tmux session：{session}"),
                ("workspace", "工作区筛选"),
                ("overview", "查看当前配置"),
                (None, "退出"),
            ],
            text=f"当前目录：{Path.cwd()}\n工作区范围：{workspace or '全部（新增使用 Linear CLI 默认工作区）'}\n全局 agent 和 tmux 为默认值，项目可覆盖；确认后即时保存。",
            default=selected,
        )
        if action is None:
            return
        selected = action
        try:
            if action == "bindings":
                manage_bindings(store, workspace)
            elif action == "prompt":
                edit_project_prompt(store, workspace)
            elif action == "agent":
                set_default_agent(store)
            elif action == "tmux":
                set_tmux_session(store)
            elif action == "workspace":
                workspace = select_workspace(store, workspace)
            elif action == "overview":
                notice(
                    "当前配置",
                    f"默认 agent：{default}\ntmux session：{session}\n工作区范围：{workspace or '全部'}\nWTR 项目：{len(rows)} 个\n数据库：{store.path}\n项目提示词目录：{prompt.directory()}",
                )
        except (Error, OSError, ValueError, sqlite3.Error) as exc:
            notice("操作失败", str(exc))
        except (KeyboardInterrupt, EOFError):
            pass
