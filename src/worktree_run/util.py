"""Process boundaries and terminal-safe display."""

import re
import shutil
import subprocess


class Error(Exception):
    """An actionable error suitable for the CLI."""


def require(command: str) -> str:
    found = shutil.which(command)
    if not found:
        raise Error(f"找不到可执行文件：{command}")
    return found


def execute(args, *, cwd=None, check=True, timeout=120):
    try:
        result = subprocess.run(
            [str(a) for a in args],
            cwd=cwd,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Error(f"执行 {args[0]} 失败：{exc}") from exc
    if check and result.returncode:
        raise Error(
            f"{args[0]} 失败 ({result.returncode})：{result.stderr.strip() or result.stdout.strip()}"
        )
    return result


def clean(text) -> str:
    # Issue titles/descriptions are untrusted terminal content.
    return re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", str(text or ""))
