"""Stable tmux object identity; names are display-only."""

import hashlib
import os
import shlex
import subprocess
import sys
from pathlib import Path

from .util import Error, execute, require

FORMAT = "#{session_id}\t#{window_id}\t#{pane_id}\t#{socket_path}"


class Tmux:
    def __init__(self, socket=None):
        require("tmux")
        self.socket = (
            socket
            or os.environ.get("WTR_TMUX_SOCKET")
            or (os.environ.get("TMUX", "").split(",")[0] or None)
        )
        self.prefix = ["tmux"]
        if self.socket:
            if not Path(self.socket).is_absolute():
                raise Error("tmux socket 必须使用绝对路径")
            self.prefix += ["-S", self.socket]
        if os.environ.get("WTR_TMUX_CONFIG"):
            self.prefix += ["-f", os.environ["WTR_TMUX_CONFIG"]]

    @property
    def endpoint(self):
        # Save the endpoint before creating a window, so a crash cannot make a later
        # invocation inside a different tmux server check the wrong server.
        if self.socket:
            return self.socket
        root = Path(os.environ.get("TMUX_TMPDIR") or "/tmp").resolve()
        return str(root / f"tmux-{os.getuid()}" / "default")

    def call(self, *args, check=True):
        return execute([*self.prefix, *args], check=check, timeout=30)

    def panes(self):
        result = self.call(
            "list-panes",
            "-a",
            "-F",
            FORMAT + "\t#{pane_dead}\t#{@wtr_run}\t#{@wtr_store}\t#{pane_start_command}",
            check=False,
        )
        if result.returncode:
            if any(
                s in result.stderr.lower()
                for s in ("no server running", "no such file", "connection refused")
            ):
                return []
            raise Error(f"无法检查 tmux 状态：{result.stderr.strip()}")
        panes = []
        for line in result.stdout.splitlines():
            parts = line.split("\t", 7)
            if len(parts) != 8:
                raise Error("tmux 返回了不支持的 pane 格式")
            panes.append(
                dict(
                    zip(
                        (
                            "session_id",
                            "window_id",
                            "pane_id",
                            "socket",
                            "dead",
                            "run",
                            "store",
                            "command",
                        ),
                        parts,
                    )
                )
            )
        return panes

    @staticmethod
    def store_token(store):
        return hashlib.sha256(str(store.path).encode()).hexdigest()

    def launch_marker(self, store, run_id):
        return f"WTR_LAUNCH_TOKEN={self.store_token(store)}:{run_id}"

    def locate(self, store, run):
        token = self.store_token(store)
        for pane in self.panes():
            tagged = pane["run"] == run["id"] and pane["store"] == token
            # Handles orchestrator dying between new-window and persisting pane IDs/tags.
            # tmux shell-quotes pane_start_command. The generated ASCII marker survives
            # that formatting, including when paths contain spaces or quotes.
            marker = self.launch_marker(store, run["id"])
            launching = bool(run["launch_command"]) and marker in pane["command"]
            if tagged or launching:
                return pane
        return None

    def tag(self, store, run_id, pane_id):
        self.call("set-option", "-p", "-t", pane_id, "@wtr_run", run_id)
        self.call("set-option", "-p", "-t", pane_id, "@wtr_store", self.store_token(store))
        self.call("set-option", "-w", "-t", pane_id, "remain-on-exit", "on")
        self.call("set-option", "-w", "-t", pane_id, "automatic-rename", "off")

    def create(self, store, run, session, name):
        source = str(Path(__file__).resolve().parent.parent)
        # Only trusted local paths/configuration and the generated UUID enter the shell.
        command = "exec " + shlex.join(
            [
                "env",
                self.launch_marker(store, run["id"]),
                "PATH=" + os.environ.get("PATH", ""),
                "PYTHONPATH=" + source,
                sys.executable,
                "-P",
                "-m",
                "worktree_run",
                "--db",
                str(store.path),
                "_run",
                run["id"],
            ]
        )
        store.update_run(run["id"], launch_command=command)
        exists = self.call("has-session", "-t", "=" + session, check=False).returncode == 0
        if exists:
            args = ["new-window", "-d", "-P", "-F", FORMAT, "-t", session + ":", "-n", name]
        else:
            args = ["new-session", "-d", "-P", "-F", FORMAT, "-s", session, "-n", name]
        output = self.call(*args, "-c", run["cwd"], command).stdout.strip().split("\t")
        if len(output) != 4:
            raise Error("tmux 未返回有效的 session/window/pane ID")
        sid, wid, pid, socket = output
        store.update_run(run["id"], session_id=sid, window_id=wid, pane_id=pid)
        store.write("UPDATE runs SET tmux_socket=? WHERE id=?", (socket, run["id"]))
        self.tag(store, run["id"], pid)
        return store.run(run["id"])

    def focus(self, run):
        target = f"{run['session_id']}:{run['window_id']}"
        print(f"run {run['id']} → {target} / {run['pane_id']}", flush=True)
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            print("后台窗口已保留：" + shlex.join([*self.prefix, "attach-session", "-t", target]))
            return
        self.call("select-window", "-t", target)
        current_socket = os.environ.get("TMUX", "").split(",")[0]
        if current_socket:
            if self.socket and current_socket != self.socket:
                print("目标位于另一 tmux server；请在外部终端 attach。")
                return
            self.call("switch-client", "-t", target)
        else:
            result = subprocess.run([*self.prefix, "attach-session", "-t", target])
            if result.returncode:
                raise Error("tmux attach 失败；后台任务仍保留")
