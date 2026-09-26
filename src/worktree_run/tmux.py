"""Stable tmux object identity; names are display-only."""

import hashlib
import os
import shlex
import subprocess
import sys
import time
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

    def locate(self, store, run):
        token = self.store_token(store)
        for pane in self.panes():
            if pane["run"] == run["id"] and pane["store"] == token:
                return pane
        return None

    @staticmethod
    def store_token(store):
        return hashlib.sha256(str(store.path).encode()).hexdigest()

    def tag(self, store, run_id, pane_id):
        self.call("set-option", "-p", "-t", pane_id, "@wtr_run", run_id)
        self.call("set-option", "-p", "-t", pane_id, "@wtr_store", self.store_token(store))
        self.call("set-option", "-w", "-t", pane_id, "remain-on-exit", "on")
        self.call("set-option", "-w", "-t", pane_id, "automatic-rename", "off")

    def create(self, store, run, session, name):
        source = str(Path(__file__).resolve().parent.parent)
        # The window hosts a normal interactive shell and wtr types its internal
        # entry point into it, so the shell and the pane survive the agent exiting.
        # Only trusted local paths and the generated UUID enter the typed command.
        env = ["-e", "PATH=" + os.environ.get("PATH", "")]
        if os.environ.get("SHELL"):
            env += ["-e", "SHELL=" + os.environ["SHELL"]]
        exists = self.call("has-session", "-t", "=" + session, check=False).returncode == 0
        if exists:
            args = ["new-window", "-d", "-P", "-F", FORMAT, "-t", session + ":", "-n", name]
        else:
            args = ["new-session", "-d", "-P", "-F", FORMAT, "-s", session, "-n", name]
        output = self.call(*args, *env, "-c", run["cwd"]).stdout.strip().split("\t")
        if len(output) != 4:
            raise Error("tmux 未返回有效的 session/window/pane ID")
        sid, wid, pid, socket = output
        store.update_run(run["id"], session_id=sid, window_id=wid, pane_id=pid)
        store.write("UPDATE runs SET tmux_socket=? WHERE id=?", (socket, run["id"]))
        self.tag(store, run["id"], pid)
        self.type_command(
            pid,
            "PYTHONPATH="
            + shlex.quote(source)
            + " "
            + shlex.join(
                [
                    sys.executable,
                    "-P",
                    "-m",
                    "worktree_run",
                    "--db",
                    str(store.path),
                    "_run",
                    run["id"],
                ]
            ),
        )
        return store.run(run["id"])

    def type_command(self, pane_id, command):
        # Wait until the shell is up, so the typed line is read as normal input
        # (terminal input stays buffered while rc files are still loading).
        deadline = time.monotonic() + 10
        while True:
            info = (
                self.call(
                    "display-message", "-p", "-t", pane_id, "#{pane_dead}\t#{pane_current_command}"
                )
                .stdout.strip()
                .split("\t")
            )
            if len(info) == 2 and info[0] == "0" and info[1]:
                break
            if time.monotonic() > deadline:
                raise Error("等待新窗口 shell 就绪超时")
            time.sleep(0.05)
        self.call("send-keys", "-t", pane_id, "-l", command)
        self.call("send-keys", "-t", pane_id, "Enter")

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
