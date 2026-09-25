# 实施与验收记录

## 范围

已实现 `PLAN.md` 第一阶段：Python 包与 `wtr` 入口、`wtr config` 统一交互配置菜单、项目配置、内置 agent 选择与默认值、Linear 查询/分页、fzf 选择/预览、提示词、worktree 创建/复用、tmux 生命周期、执行记录、恢复检查、dry-run。

第二阶段 `poll --once`、第三阶段 `watch` 按计划作为后续工作；当前没有无人值守调度、超时/重试策略或 Linear 状态回写。

## 本机验证

环境：macOS arm64、Python 3.11.15、Linear CLI 2.3.0、Git 2.55.0、tmux 3.7b、fzf 0.74.1、pi 0.81.1、Codex CLI 0.147.0、uv 0.11.31。

### 真实 Linear（只读）

- `linear api` 返回 `{data: ...}` JSON；GraphQL 错误通过退出状态和 `errors` 双重检查。
- `viewer.id`、`organization.id/urlKey` 可查询；以返回的 urlKey 显式指定 `--workspace` 后再次核对组织 UUID。
- `issues` 支持 `assignee.isMe.eq` 和排除 completed/canceled/duplicate 的状态过滤；通过真实 API 和实际 `Linear.mine()` 查询验证重复任务不进入交互列表。
- `issue(id: $id)` 可用 identifier 查询并返回 UUID、identifier、项目、状态、描述等字段。
- `projects(first: 1, after: $after)` 实际翻至第二页，游标前进且记录不同。
- 不依赖 `linear issue list` 的人类可读表格，不读取/输出认证 token，不修改远端 issue。

### 自动化测试

```bash
uv run --python 3.11 python -m unittest discover -s tests -v
WTR_TEST_PI=1 uv run --python 3.11 python -m unittest discover -s tests -p test_pi.py -v
uvx ruff check src tests
uvx ruff format --check src tests
uv build
```

当前默认回归测试：**94 项通过，1 项真实 pi 用例按需跳过**；真实 pi 用例单独运行也通过。默认测试不调用真实 Linear/model，需要安装 Git、tmux 和 fzf。

覆盖：

- 配置菜单的方向键/数字选择、Emacs/Vim 导航及首尾跳转、搜索模式与 j/k/gg/G/q 文字输入隔离、保留筛选返回导航、项目搜索、连续修改设置、默认 agent 设置/清除、预填字段、校验失败后重试与取消、工作区范围、模板编辑、绑定/解除的确认；真实隔离 tmux 验证完整流程。菜单和编辑器等待期间不持有任务启动锁，并发修改项目配置时拒绝覆盖，脚本命令保持可用。
- GraphQL 分页、重复游标、错误响应、当前用户过滤和无优先级排序；每页保留终态筛选，已完成/取消/重复任务排除，选中后变为重复任务时阻止启动。
- 项目路由、跨 workspace 重名消歧、无项目 fallback、配置删除保留关联和历史。
- `project add` 从 main worktree 根目录绑定当前目录，在 Linear 查询前校验位置；覆盖子目录、附加 worktree、裸仓库、非仓库、独立 Git 元数据目录及主工作区检出其他分支。
- 真实终端中的 issue worktree 路径输入：预填当前仓库目录，回车确认、原位修改（含中文和空格）、取消不保存；编辑项目时预填已有绑定目录。
- 内置 agent 的命令/参数、安装检测、默认值、单次指定与真实终端选择菜单、取消选择、缺失可执行文件、旧数据库兼容；五种适配器均通过假可执行程序和真实 tmux 验证 cwd/TTY/参数传递。
- 项目模板按 workspace/project ID 隔离；显式项目选择、当前仓库/关联 worktree 识别、共用仓库的消歧、无需 Linear 网络的编辑、删除再绑定保留内容、旧文件保留、无项目默认模板、issue 迁移项目后的路由及执行批次快照。
- 模板占位符、中文、引号、换行、shell 元字符和约 60 KB 的单参数提示词。
- 真实临时 Git 仓库：创建、复用、脏 main worktree、路径占用、分支冲突、其他 worktree 检出、失效基线、fetch 失败、远端引用刷新、同步 post-checkout hook 完成。
- 独立 tmux server：cwd/TTY、并发启动只认领一次、改名后去重、成功/失败退出码、退出输出保留、重跑新批次、pane 消失恢复、IDs/tags 缺失恢复、仓库同名模块不影响内部入口。
- 真实 fzf：描述预览、取消无执行副作用、选择后进入执行流程。
- dry-run 数据库字节不变，不创建模板、worktree、分支、tmux socket 或执行批次。
- tmux 内外焦点分支通过 mock 验证；无 TTY 后台行为通过真实子进程验证。

### 内置 Agent 参数依据

| 适配器 | 核实依据 |
| --- | --- |
| pi | 本机 `pi --help` 和真实交互测试：`pi <prompt>` |
| Codex | 本机 `codex --help`：无子命令时为交互 CLI，位置参数为初始提示词 |
| Claude Code | [官方 CLI 文档](https://code.claude.com/docs/en/cli-reference)：`claude "query"` 启动带初始提示词的交互会话 |
| OpenCode | [官方 CLI 文档](https://opencode.ai/docs/cli/#tui)：TUI 使用 `--prompt` |
| Gemini CLI | [官方 CLI 文档](https://geminicli.com/docs/cli/cli-reference/)：`--prompt-interactive` 执行初始提示词并继续交互 |

### 真实 pi 交互 smoke test

使用临时 Git 仓库、测试 issue 标识和独立 tmux server，启动真实 pi 交互模式。为测试禁用工具、仓库上下文、扩展、skills、模板及会话持久化；发送包含 issue identifier 的初始提示词，模型正确返回要求拼接的字符串。发送 `/quit` 后记录退出码 0 和 `awaiting_review`，输出保留。

这验证了内置 pi 适配器的交互参数与提示词行为；测试没有启动任何真实 issue 的开发。日常使用通过 `--agent pi` 或 `wtr config set agent pi` 选择。

### 安装产物

- wheel 和 sdist 构建成功。
- 在临时 `UV_TOOL_DIR` / `UV_TOOL_BIN_DIR` 中执行 `uv tool install <wheel>`。
- 从 `/` 目录调用安装后的 `wtr`，通过假 Linear 路由到临时仓库，真实 tmux 启动假 agent，并记录退出成功。
- 未修改用户现有的全局 wtr 安装、项目绑定或 tmux 配置；所有测试 server 均为独立 socket 并在结束时清理。

## 验收边界

- 本机未安装 Claude Code、OpenCode、Gemini CLI，其参数已按官方文档核对，并用假可执行程序验收适配器。Codex 已核对本机帮助；这些 CLI 尚未进行真实模型的交互端到端验收。
- 未指定实际业务仓库，因此没有验证其宿主 Monitor、Secrets、端口或依赖初始化结果。同步 Git hook 已验证会在启动前完成；异步准备需由项目提供显式就绪检查/agent 包装程序。
- 生产使用前需绑定实际项目，并在该仓库做一次人工端到端验收，确认终端焦点和宿主环境就绪。
- 进程退出仅作为执行信号，不代表代码或 issue 已完成；不自动提交、创建 PR 或变更 Linear 状态。
