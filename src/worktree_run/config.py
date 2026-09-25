"""Validation shared by interactive and non-interactive configuration."""

import re

from . import agents
from .util import Error


def session_name(name):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        raise Error("tmux session 名称只能包含字母、数字、下划线和连字符")
    return name


def set_value(store, key, value):
    """Validate a setting before saving; caller holds the orchestration lock."""
    if key == "agent":
        agents.get(value)
    elif key == "tmux-session":
        session_name(value)
    else:
        raise Error(f"未知设置：{key}")
    store.set_setting(key, value)
