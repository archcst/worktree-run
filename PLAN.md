# worktree-run 实施计划

CLI 命令：`wtr`

## 目标

从 Linear 中选择分配给自己的 issue，根据其项目路由到本地仓库，创建或复用独立 worktree，并在统一 tmux session 的 issue 专属 window 中启动 CLI coding agent，携带包含 issue ID 的初始提示词开始开发。

未来支持自动轮询分配给自己的 issue，复用同一套任务准备和执行逻辑。

## 技术方案

- Python 3.11+，优先使用标准库。
- `argparse`：命令行入口。
- `prompt-toolkit`：交互配置菜单、搜索选择和可编辑的预填输入。
- `subprocess`：调用 Linear、Git、tmux、agent CLI。
- `sqlite3`：保存配置、issue 关联和执行记录。
- `pathlib`：路径处理。
- `fzf`：交互搜索和选择 issue。
- 使用 `pyproject.toml` 声明 `wtr` 入口，可通过 `uv tool install` 安装。

外部依赖：`linear`、`git`、`tmux`；交互选择需要 `fzf`，执行需要已配置的 agent CLI。

## 核心流程

1. 检查本次运行需要的依赖和配置。
2. 通过 Linear CLI 查询分配给当前用户的未完成 issues，或读取指定 issue。
3. 使用 fzf 搜索选择，显示 issue ID、标题、项目、状态和优先级，并提供描述预览。
4. 根据 workspace 和 project ID 查找本地项目绑定。
5. 校验仓库、基线和目标路径，创建或复用 issue worktree。
6. 将提示词模板中的 `{{issue_id}}` 替换为 Linear issue identifier，例如 `ENG-123`。
7. 在统一 tmux session 中创建或定位该 issue 的 window。
8. 在 worktree 目录启动选定 agent，将完整提示词作为参数传入。
9. 保存执行记录，并将用户切换到目标 window。

手动按 ID 启动允许明确指定可访问的 issue；交互列表和未来轮询默认限定为分配给自己的 issue。

## 项目路由和 worktree

### 项目绑定

通过 `wtr config` 的项目管理菜单配置并保存在 SQLite，参数化项目命令用于脚本和快捷操作：

- Linear workspace 身份。
- Linear project ID 和显示名称。
- 本地 main worktree 绝对路径。
- 新分支基线，例如 `origin/main`。
- issue worktree 根目录。

按项目名称选择绑定，内部使用 workspace + project ID 定位，避免重名和改名问题。

未绑定或没有项目的 issue：手动模式引导选择仓库；仅对有项目的 issue 提供持久化项目绑定。自动模式跳过并记录原因。

### 创建与复用

- main worktree 用作仓库入口，分支起点使用独立配置的基线。
- 远端基线在创建前 fetch 对应 remote；失败时明确报错，不静默使用旧引用。
- 不需要切换或 pull main worktree，不搬运其未提交修改。
- 建议分支名：`issue/eng-123`。
- 建议路径：`<worktree-root>/eng-123`。
- issue 身份包含 workspace；共享仓库或目录中的重名必须检测并消歧。
- 再次启动同一 issue 时，校验仓库和分支后复用关联 worktree。
- 遇到已有分支、路径占用或分支已在其他 worktree 检出时，明确处理冲突，不能覆盖、重置或删除已有内容。
- worktree 创建后由现有宿主环境自动化处理环境准备；如存在异步就绪要求，在实现验收时确认实际行为。
- 后续步骤失败时保留已创建 worktree，并记录可重试的状态。

## 配置与存储

`wtr config` 提供统一的交互配置入口，包括项目管理、项目提示词、默认 agent、tmux session、工作区筛选和配置概览。支持方向键、Emacs（Ctrl+n/p、Alt+</>、Ctrl+g）和 Vim（j/k、gg/G、q）菜单导航，Enter 确认。项目列表按 / 进入搜索模式，Esc/Ctrl+g 返回导航并保留筛选；支持预填输入、取消和返回。修改确认后即时保存，解除绑定需再次确认；菜单和外部编辑器等待期间不持有任务启动锁。

配置和执行记录存入 SQLite；保留 `wtr config list/set` 等参数化命令供脚本使用。工作区筛选只影响本次配置操作范围。

建议位置：

```text
~/.local/share/worktree-run/state.db
~/.config/worktree-run/prompts/<workspace-id>/<project-id>.md
```

### 逻辑数据

- `projects`：项目与仓库的绑定。
- `settings`：默认 agent、tmux session 名称；未来增加轮询设置。
- 内置 agent 适配器：名称与交互启动命令由程序维护；SQLite 仅保存默认名称和各执行批次的实际参数。
- `issues`：workspace、issue ID/identifier、项目、仓库、分支、worktree 关联。
- `runs`：执行批次、agent、状态、时间、退出信息、tmux 标识及可用的 agent 会话信息。

一次 issue 可有多个执行批次，worktree 关联与执行批次分开保存。agent 会话关联仅在对应 CLI 提供可靠能力时记录，不假定所有工具具有统一的会话接口。

Linear 认证复用 Linear CLI。配置初始化和数据库变更需要保持简单、可重复执行。

## 提示词模板

每个 Linear 项目使用独立的 Markdown 模板，以 workspace ID + project ID 定位。新建绑定、首次编辑或实际启动时初始化默认模板。通过 `wtr prompt edit` 使用用户编辑器修改当前目录对应的项目模板，也可用 `wtr prompt edit <项目名称或ID>` 显式选择。一个仓库绑定多个项目时需要选择，项目改名不影响模板关联。

```markdown
请开发 Linear issue {{issue_id}}。

通过 linear CLI 阅读需求和讨论，遵循仓库开发规范，
完成实现和必要测试，最后总结改动与验证结果。
```

- 第一版只支持 `{{issue_id}}`，使用字符串替换。
- 启动时使用 issue 当前所属项目的模板；无项目的 issue 使用内置默认提示词。
- 项目模板相互独立；修改/删除绑定不覆盖或删除已有模板。提示词快照随执行批次保存，编辑模板不影响已启动批次。
- `--dry-run` 只读取模板，缺少项目模板时预览内置默认内容，不创建文件。
- 启动前检查占位符存在，保证提示词包含目标 issue。
- issue identifier 必须来自已验证的 Linear 记录。
- 多 workspace 环境下，为 agent 提供明确的 workspace 上下文，防止后续查询定位到错误 workspace；具体方式随 Linear CLI 能力验证。

## Agent 启动

程序内置 agent 适配器，用户安装并认证对应 CLI 后即可选择使用。

| agent | 交互启动命令 |
| --- | --- |
| pi | `pi <prompt>` |
| codex | `codex <prompt>` |
| claude | `claude <prompt>` |
| opencode | `opencode --prompt <prompt>` |
| gemini | `gemini --prompt-interactive <prompt>` |

- 各适配器生成参数数组，完整渲染后的提示词作为单个参数传递，不经过 shell 拼接。
- agent 继承终端，cwd 为 issue worktree。
- 通过 `wtr agent list` 查看内置适配器、安装状态和默认值。
- 支持 `wtr config set agent NAME` 保存默认值，以及单次 `--agent NAME` 覆盖。
- 未设置默认值或使用不带名称的 `--agent` 时，交互选择已安装的 agent；单次选择不修改默认值。
- 保留各 agent 自身的模型、认证及权限设置，不添加自动批准参数。
- 分别核实各 CLI 的交互参数和初始提示词行为。第二阶段由适配器提供单独验证的无人值守启动方式。

## tmux 行为

默认 session 名称：`worktree-run`。

```text
worktree-run
├── ENG-123  → issue worktree → pi
├── ENG-145  → issue worktree → opencode
└── APP-28   → issue worktree → pi
```

- session 不存在时创建。
- 每个 issue 对应一个 window，显示名优先使用 issue identifier，必要时添加 workspace 消歧。
- 使用 tmux 返回的 session/window/pane ID 保存关联；不依赖可修改的显示名识别任务。
- 创建 window 时指定工作目录。
- issue 已有存活的执行窗口时只定位，不重复启动 agent。
- tmux 外运行时 attach；tmux 内运行时切换当前客户端，避免嵌套。
- 无可交互终端时保留后台窗口并输出定位信息，不强行 attach。
- agent 退出后保留输出，区分已退出窗口与正在执行窗口；重跑使用新的执行批次。
- 每次读取关联都校验 tmux 对象仍然存在，窗口关闭后可重新启动。
- 未来自动模式只创建窗口，不抢占当前客户端焦点。
- 使用稳定的内部执行入口（例如 `wtr _run <run-id>`）在窗口中读取已保存参数并启动 agent，避免把 issue 文本直接嵌入 tmux shell 命令。内部入口的具体形式在实现时确定。
- 包装执行进程负责记录退出码和结束状态；异常关闭窗口或进程崩溃后，通过恢复检查校正记录。

## CLI 草案

```bash
wtr config                  # 打开统一配置菜单
wtr                         # 交互选择我的 issue
wtr ENG-123                 # 启动指定 issue
wtr ENG-123 --agent opencode # 单次选择 agent
wtr --dry-run               # 预览路由、worktree 和启动计划，不执行创建

wtr project add             # 交互绑定 Linear 项目和仓库
wtr project list
wtr project edit
wtr project remove

wtr agent list              # 查看内置 agent、安装状态和默认值
wtr ENG-123 --agent          # 本次交互选择已安装的 agent

wtr config set agent pi
wtr config set tmux-session worktree-run
wtr config list
wtr prompt edit             # 编辑当前目录对应的项目模板，否则选择项目
wtr prompt edit PROJECT_ID  # 显式选择项目
wtr runs
```

首次缺少项目绑定时提供交互引导，未设置默认 agent 时提供选择菜单。删除项目配置或修改默认 agent 不删除 worktree 或历史执行记录。

未来增加：

```bash
wtr poll --once             # 查询并调度一次
wtr watch                   # 持续轮询
wtr retry ENG-123           # 显式重新入队，具体接口后续确定
```

## 自动轮询阶段

### 接单范围

- 仅处理分配给当前 Linear 用户的 issue。
- 必须属于已绑定项目并满足配置的待办状态。
- 可选要求 `agent-ready` 标签。
- 优先级高的先执行，同优先级按创建时间；正确处理 Linear 的“无优先级”值。
- 初始并发为 1。
- 调度前重新核实 assignee 和执行条件。

### 状态和可靠性

计划状态：

```text
queued → preparing → running → awaiting_review
                       ├── blocked
                       └── failed
```

- 同一 issue 的运行中、待审核或阻塞任务不重复启动。
- issue 描述更新不会自动触发重跑。
- 本地唯一约束和事务认领避免手动命令与轮询同时重复执行。
- 自动执行设超时和重试上限。
- 进程退出只是执行信号，不代表 issue 已完成；成功退出进入待审核，记录验证结果的能力需按 agent 协议确认。
- 无人值守执行遇到需要用户决策的情况，应通过明确的结果协议标记阻塞；超时本身不能当成成功或准确的阻塞原因。
- 工具重启后核实遗留窗口和进程，再恢复调度，不能直接重复启动。
- 第一阶段保证单机协调；跨机器并发需另行设计共享认领机制。
- Linear 状态更新、提交和 PR 发布如需自动化，后续单独定义策略。

## 建议代码结构

```text
pyproject.toml
src/worktree_run/
  __init__.py
  cli.py          # CLI 和交互入口
  config.py       # 配置访问和校验
  config_ui.py    # 统一交互配置菜单
  agents.py       # 内置 agent 启动适配器与安装检测
  store.py        # SQLite 与执行记录
  linear.py       # Linear CLI 适配和数据解析
  worktree.py     # 路由和 Git worktree
  prompt.py       # 模板读取与替换
  runner.py       # agent 进程与执行状态
  tmux.py         # session/window 管理
  watch.py        # 后续增加轮询调度
tests/            # 测试
```

## 实施阶段

### 第一阶段：手动开发闭环

1. 验证本机 Linear CLI 的结构化输出、分页、当前用户、项目和 issue 查询接口。
2. 建立 Python 包、`wtr` 入口和 SQLite 最小表结构。
3. 实现统一配置菜单：项目绑定、内置 agent 选择与默认值、项目提示词编辑及 tmux 设置。
4. 实现 issue 列表和 fzf 选择。
5. 实现 worktree 创建、校验与复用。
6. 实现 tmux session/window 和 agent 启动、退出记录。
7. 完成 dry-run、清晰的错误反馈和测试。

### 第二阶段：单次自动调度

实现 `wtr poll --once`、agent 自动启动方式、事务认领、超时与待审核/失败/阻塞结果处理。

### 第三阶段：持续轮询

实现 `wtr watch`、恢复检查、受限重试和可配置调度策略。

## 测试与验收

- 单元测试覆盖项目路由、模板替换、参数边界、状态转换及去重。
- 临时 Git 仓库覆盖创建、复用、路径占用、已有分支、基线失效和工作区有未提交内容的情况。
- 假 Linear/agent 可执行文件覆盖错误退出、分页、长提示词、中文、空格、引号及换行；查询列表不能依赖脆弱的人类可读表格解析。
- 使用隔离 tmux server 验证 window cwd、参数传递、重复启动、退出保留及遗留状态恢复。
- 验证取消 fzf 不会创建 worktree 或运行记录中的执行任务。
- `--dry-run` 不创建分支、worktree、tmux window 或执行批次，不运行 agent。
- 使用内置 pi 适配器验证交互闭环；其他 agent 在对应 CLI 可用时按其参数完成兼容性验收。
- 验证新 worktree 的实际环境可用性，明确任何宿主自动化的时序限制。

完成第一阶段的标准：用户能在任意目录运行 `wtr`，选中自己的 issue，进入正确仓库的独立 worktree 对应 tmux window，并看到 agent 使用含该 issue ID 的提示词开始工作；重复选择不会重复开工。
