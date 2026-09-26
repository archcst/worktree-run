"""Built-in interactive coding-agent adapters."""

import shutil
from dataclasses import dataclass
from pathlib import Path

from .util import Error


@dataclass(frozen=True)
class Agent:
    name: str
    label: str
    command: tuple[str, ...]

    def argv(self, prompt):
        # Every adapter passes the complete prompt as exactly one argument.
        return [*self.command, prompt]

    def executable(self):
        found = shutil.which(self.command[0])
        return str(Path(found).absolute()) if found else None

    def launch_argv(self, prompt):
        executable = self.executable()
        if not executable:
            raise Error(
                f"agent {self.name} 未安装或不在 PATH；请安装 {self.command[0]} 后重试（wtr agent list 查看可用项）"
            )
        argv = self.argv(prompt)
        argv[0] = executable
        return argv


BUILTINS = {
    agent.name: agent
    for agent in (
        Agent("pi", "pi", ("pi", "--approve")),
        Agent("codex", "OpenAI Codex", ("codex",)),
        Agent("claude", "Claude Code", ("claude",)),
        Agent("opencode", "OpenCode", ("opencode", "--prompt")),
        Agent("gemini", "Gemini CLI", ("gemini", "--prompt-interactive")),
    )
}


def get(name):
    if name not in BUILTINS:
        raise Error(
            f"未知 agent：{name}；可选：{', '.join(BUILTINS)}。使用 --agent 指定，或 wtr config set agent <名称> 设置默认值"
        )
    return BUILTINS[name]


def catalog(default=None):
    rows = []
    for agent in BUILTINS.values():
        executable = agent.executable()
        rows.append(
            dict(
                name=agent.name,
                label=agent.label,
                command=agent.argv("{{prompt}}"),
                installed=bool(executable),
                executable=executable,
                default=agent.name == default,
            )
        )
    return rows
