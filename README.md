# worktree-run

从 Linear 选择 issue，在独立 Git worktree 的 tmux window 中启动 coding agent。
当前实现 `PLAN.md` 第一阶段（手动闭环），Python 3.11+。交互配置菜单和路径编辑使用 `prompt-toolkit`，安装时自动提供。

## 安装

```bash
uv tool install .
wtr --help
```

外部依赖：`linear`（已验证 2.3.0 的 `api` 接口）、`git`、`tmux`；交互选 issue 需要 `fzf`。先通过 `linear auth login` 认证。agent 需自行安装和认证。

## 配置和使用

```bash
cd /path/to/main-worktree
wtr config
```

在菜单中统一完成配置：

```text
wtr 配置
├── 项目管理         绑定当前仓库、编辑基线和 worktree 目录、解除绑定
├── 项目提示词       选择项目后打开编辑器
├── 默认 agent       查看安装状态、设置默认值或改为每次选择
├── tmux session     修改新任务使用的 session 名称
├── 工作区筛选       选择本次配置的项目范围
├── 查看当前配置     查看设置与配置文件位置
└── 退出
```

菜单同时支持方向键、Emacs 和 Vim 快捷键，无需切换配置：

| 操作 | 快捷键 |
| --- | --- |
| 下一项 / 上一项 | `↓ / ↑`、`Ctrl+n / Ctrl+p`、`j / k` |
| 第一项 / 最后一项 | `Home / End`、`Alt+< / Alt+>`、`gg / G` |
| 确认 | `Enter` |
| 返回或退出当前菜单 | `Esc`、`Ctrl+g`、`q`（也支持 `Ctrl+C`） |
| 普通菜单定位 | 数字键 |
| 项目列表搜索 | `/` 进入搜索，`Ctrl+U` 清空 |

搜索输入期间，`j/k/g/G/q` 都是普通文字，仍可使用方向键或 `Ctrl+n/p` 选择结果、Enter 确认。按 `Esc` 或 `Ctrl+g` 返回导航模式并保留筛选，再按一次返回上级菜单。

这些快捷键用于菜单导航；文本输入框预填当前值，路径支持 Tab 补全。确认后即时保存；取消当前输入不会保存该次修改，返回/退出不会撤销已经保存的设置。提示词以编辑器保存结果为准。

可以在任意目录打开菜单管理已有配置；新增绑定或重新绑定当前仓库时，需要从 main worktree 根目录运行 `wtr config`。修改项目设置和解除绑定只影响新任务，保留已有 worktree、提示词和执行记录。解除绑定需要单独确认。

工作区筛选仅在本次配置期间生效，不改变 Linear CLI 的默认工作区。打开菜单、编辑已有配置和提示词均无需查询 Linear；只有新增项目绑定会查询 Linear。菜单和编辑器等待期间不会持有任务启动锁。

配置完成后：

```bash
wtr                             # 搜索分配给自己的未完成 issue
wtr ENG-123                     # 启动指定 issue
wtr ENG-123 --agent codex       # 本次指定 agent
wtr ENG-123 --agent             # 本次交互选择 agent
wtr ENG-123 --agent pi --dry-run
wtr ENG-123 --no-attach          # 创建后台窗口
wtr runs
```

交互 issue 列表只包含分配给当前 Linear 用户的未完成任务，排除 `completed`、`canceled` 和 `duplicate` 状态；选中后会再次核对状态和负责人。手动指定 issue 编号仍可访问该任务。

多工作区：任何命令均可加 `--workspace <slug>`。启动时查询组织 UUID，之后显式传递 workspace slug；提示词包含英文的 workspace 上下文（slug、组织 UUID），并要求所有 Linear 查询显式使用 `--workspace`。不会修改 Linear 当前默认工作区，也不会更新 issue 状态。

### 脚本与快捷配置

也可使用参数化命令完成配置。`wtr project add` 必须在 Git **主 worktree 的根目录**执行，自动使用当前目录。这里的 main worktree 指仓库的主工作区，与当前检出的分支名称无关。在子目录或附加 worktree 中运行会提示应进入的主工作区路径。

issue worktree 根目录默认是当前仓库目录。路径会预填到输入框，可直接编辑、按 Tab 补全目录，或回车确认。例如在 `/projects/my-app` 中绑定时，预填 `/projects/my-app`，issue worktree 会创建在其子目录中（如 `/projects/my-app/eng-123`）。`project edit` 预填已保存的目录；也可用 `--root` 显式指定。

```bash
cd /absolute/main-worktree
wtr --workspace my-team project add PROJECT_ID \
  --baseline origin/main --root /absolute/issue-worktrees
wtr project list
wtr project edit PROJECT_ID --baseline origin/develop
wtr project remove PROJECT_ID
wtr config set agent pi
wtr config set tmux-session worktree-run
wtr config list
wtr agent list
```

未绑定项目/无项目的 issue 在交互终端中引导选择仓库；有项目时可选择保存绑定。已有 issue 关联始终复用原仓库；修改/删除项目绑定只影响新 issue，不删除任何 worktree 或历史记录。

## 项目提示词

在 `wtr config` 的「项目提示词」菜单中选择项目编辑。每个 Linear 项目有独立的 Markdown 模板，以 workspace ID + project ID 定位；项目重名、改名或共用仓库都不会混用模板。也可直接调用：

```bash
wtr prompt edit                  # 在仓库或已关联的 issue worktree 内识别项目
wtr prompt edit "项目名称"         # 在任意目录指定绑定项目（也可用 project ID）
wtr --workspace my-team prompt edit PROJECT_ID
EDITOR=nano wtr prompt edit
```

无法从当前目录识别项目时会提供项目选择；一个目录绑定多个项目时也需选择。编辑过程只读取本地绑定，不需要查询 Linear。编辑器使用 `VISUAL`、`EDITOR` 或 `vi`，命令会显示实际模板路径。

新建绑定、首次编辑或实际启动时，为项目初始化默认模板；编辑已有项目配置或重新绑定同一项目会保留其模板内容。模板必须包含 `{{issue_id}}`，例如：

```markdown
请开发 Linear issue {{issue_id}}。

遵循本项目的 AGENTS.md；完成实现后运行项目测试，
最后总结改动、验证结果和待确认事项。
```

启动时按 issue 当前所属项目读取模板，自动附加 Linear workspace 上下文；提示词快照保存在执行批次中，编辑模板只影响之后的新批次。`--dry-run` 可预览最终参数，不创建模板文件。无项目的 issue 使用内置默认提示词。

模板位置：`${XDG_CONFIG_HOME:-~/.config}/worktree-run/prompts/<workspace-id>/<project-id>.md`。删除项目绑定不会删除模板。

已有的 `~/.config/worktree-run/prompt.md` 文件保持原样。若其中有需要沿用的内容，请通过 `wtr prompt edit <项目>` 打开目标项目模板，再将内容复制进去；新批次读取项目专属文件。

## 内置 Agent

启动命令由程序内置适配，使用各 CLI 的交互模式：

| 名称 | 启动方式（提示词为单个参数） |
| --- | --- |
| `pi` | `pi <prompt>` |
| `codex` | `codex <prompt>` |
| `claude` | `claude <prompt>` |
| `opencode` | `opencode --prompt <prompt>` |
| `gemini` | `gemini --prompt-interactive <prompt>` |

先安装并认证需要使用的 CLI。`wtr agent list`（或 `wtr agent`）显示内置命令、PATH 中的可执行文件、安装状态与默认值。

- `--agent NAME`：本次指定 agent，优先于默认值。
- `--agent`：本次打开选择菜单，列出已安装的 agent。
- 不传 `--agent`：使用配置菜单中保存的默认值；未设置时交互选择。也可用 `wtr config set agent NAME` 保存默认值。
- 交互选择和单次覆盖不修改默认值。无交互终端时需要明确名称或已有默认值。
- 模型、认证、权限遵循各 agent 自身的设置；wtr 不添加自动批准或绕过安全检查的参数。
- 已有活动任务只定位窗口，不切换其 agent；新批次使用本次选择。

已有数据库中的内置 agent 默认名称可继续使用。旧自定义启动配置保留在数据库，但不覆盖内置适配器；历史和活动批次仍保留其原始参数。默认名称不受支持时，可用 `wtr config set agent NAME` 更新。

## 执行与恢复

- 远端基线创建前显式 fetch 对应远端分支；失败不会使用缓存旧引用。无需切换、pull 或清理 main worktree。
- 路径、已有分支和其他 worktree 冲突会报错，不覆盖、重置或删除内容。已知跨 workspace 重名使用稳定 hash 后缀消歧。
- 同一数据库通过文件锁协调准备过程，并用 SQLite 唯一索引约束同 issue 的活动执行。请让同机调用共享数据库；跨数据库/跨机器不提供协调。
- 保存 tmux socket、session/window/pane ID，并用执行 UUID 标记 pane，不依赖窗口名称；已有活动窗口仅定位。
- 包装进程记录退出码：0 为 `awaiting_review`，非 0 为 `failed`，**不代表 issue 已完成**。窗口本身是普通交互 shell，wtr 把内部执行命令输入该窗口运行；agent 退出后回到提示符，可用 ↑ 重跑。再次手动启动创建新批次。用户退出 shell 时窗口按 `remain-on-exit` 保留输出。
- `wtr runs` 和启动前会校验遗留 pane，将消失/异常退出的活动记录标记为 `interrupted`；仍存活的 pane 不重复启动。
- 创建失败或后续失败保留 worktree/关联和错误记录。解决冲突后重试相同命令。异常退出若留下存活 pane，需先检查该 pane 再自行关闭，避免重复工作。
- 无交互终端时不 attach；在 tmux 内切换当前客户端，在外部 attach。另一个 tmux server 中的窗口仅输出定位信息。
- `--dry-run` 只读数据库，不写配置/模板，不 fetch、不创建分支、worktree、window 或运行记录、不执行 agent；缺少项目绑定时提示先配置；agent 可通过 `--agent NAME` 或默认值指定。

Git 同步 hooks 完成后才启动 agent。宿主 Monitor 或 hooks 派生的**异步**环境初始化没有通用就绪协议，本版不会猜测就绪或自动等待。项目需确保环境同步准备完成；需要异步初始化的项目，应在启动 agent 前完成明确的 readiness 检查。

## 数据与隔离

- 数据库：`${XDG_DATA_HOME:-~/.local/share}/worktree-run/state.db`，可用 `--db` 覆盖。
- 项目提示词：`${XDG_CONFIG_HOME:-~/.config}/worktree-run/prompts/<workspace-id>/<project-id>.md`。
- 模板只替换 `{{issue_id}}`；启动前必须存在此占位符。
- 数据库保存提示词/参数和任务历史（可能含敏感信息），新数据库权限为 `0600`。agent 参数中不要存明文密钥。
- `WTR_TMUX_SOCKET` 可指定独立 server 的绝对 socket 路径；`WTR_TMUX_CONFIG` 可指定新 server 使用的 tmux 配置。默认沿用当前 tmux server 或系统默认 server。
- agent 继承 pane 终端和环境，设置 `WTR_LINEAR_WORKSPACE`、`WTR_RUN_ID`，使用发起进程的 PATH 和解析后的 agent 可执行路径。认证环境变量遵循 tmux server 的环境语义。

## 开发与验收

```bash
uv run --python 3.11 python -m unittest discover -s tests -v
uv build
```

测试使用临时 Git 仓库、假 Linear/agent 及独立 tmux server，不操作真实 issue 或已有工作目录。实际验收记录见 [VALIDATION.md](VALIDATION.md)。

后续按计划增加 `poll --once` 和 `watch`，包括自动执行协议、超时、调度状态、有限重试及无人值守就绪检查。
