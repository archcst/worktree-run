"""Manual orchestration and the stable in-pane execution entry point."""

import json
import os
import signal
import subprocess

from . import agents, config, prompt, worktree
from .store import now
from .tmux import Tmux
from .util import Error, require


def recover(store, workspace=None, issue_id=None):
    """Caller holds the orchestration lock. A live pane is never relaunched."""
    rows = store.all("SELECT * FROM runs WHERE status IN ('preparing','running')")
    for run in rows:
        if workspace is not None and (run["workspace"], run["issue_id"]) != (workspace, issue_id):
            continue
        pane = Tmux(run["tmux_socket"]).locate(store, run)
        if pane and pane["dead"] == "0":
            store.update_run(
                run["id"],
                session_id=pane["session_id"],
                window_id=pane["window_id"],
                pane_id=pane["pane_id"],
            )
            continue
        # The wrapper can finish while tmux is being inspected. Never overwrite its
        # terminal result with a recovery result based on an earlier snapshot.
        store.write(
            """UPDATE runs SET status='interrupted',ended_at=?,error=?
               WHERE id=? AND status IN ('preparing','running')""",
            (now(), "执行窗口已关闭或包装进程异常退出；worktree 保留，可重新启动", run["id"]),
        )


def plan(store, linear, issue, binding, agent_name):
    row = worktree.route(store, linear, issue, binding)
    action = worktree.inspect(row, associated=bool(store.issue(linear.workspace, issue["id"])))
    agent = agents.get(agent_name)
    text = prompt.render(issue["identifier"], linear.slug, linear.workspace, row["project_id"])
    # Resolve once against the launcher's PATH, not an old tmux server environment.
    argv = agent.launch_argv(text)
    session = config.session_name(store.setting("tmux-session", "worktree-run"))
    require("tmux")
    return row, action, argv, session


def launch(store, linear, issue, binding, agent_name):
    """Caller holds the orchestration lock (including recovery and active check)."""
    row, _, argv, session = plan(store, linear, issue, binding, agent_name)
    tmux = Tmux()
    associated = bool(store.issue(linear.workspace, issue["id"]))
    store.save_issue(row)
    run_id = store.new_run(row, agent_name, argv, tmux.endpoint)
    try:
        prompt.initialize(row["workspace"], row["project_id"])
        worktree.prepare(row, associated=associated)
        name = issue["identifier"]
        others = store.all(
            "SELECT workspace FROM issues WHERE identifier=? AND workspace<>?",
            (name, linear.workspace),
        )
        if others:
            name += "-" + linear.workspace.replace("-", "")[:8]
        return tmux.create(store, store.run(run_id), session, name)
    except BaseException as exc:
        # If window creation succeeded but acknowledgement failed, do not release the claim.
        run = store.run(run_id)
        try:
            pane = tmux.locate(store, run)
        except Error:
            pane = True  # Uncertain liveness: preserve claim for recovery, not duplicate work.
        if not pane:
            store.update_run(run_id, status="failed", ended_at=now(), error=str(exc))
        else:
            store.update_run(run_id, error=str(exc))
        raise


def internal_run(store, run_id):
    run = store.run(run_id)
    if not run:
        raise Error("执行记录不存在")
    pane_id = os.environ.get("TMUX_PANE")
    if not pane_id:
        raise Error("内部执行入口只能在关联 tmux pane 内运行")
    tmux = Tmux(os.environ.get("TMUX", "").split(",")[0] or run["tmux_socket"])
    # The wrapper runs inside the shell window wtr created; claim the pane before
    # the parent finishes its setup transaction (covers a crash between the two).
    owner = tmux.call("show-options", "-v", "-p", "-t", pane_id, "@wtr_run", check=False)
    if owner.stdout.strip() not in ("", run_id):
        raise Error("当前 tmux pane 不属于该执行批次")
    tmux.tag(store, run_id, pane_id)
    with store.lock():
        run = store.run(run_id)
        if run["status"] != "preparing":
            raise Error("执行批次已启动或结束，拒绝重复执行")
        worktree.inspect(store.issue(run["workspace"], run["issue_id"]), associated=True)
        pane = next((p for p in tmux.panes() if p["pane_id"] == pane_id), None)
        if pane is None:
            raise Error("执行窗口已关闭")
        store.write("UPDATE runs SET tmux_socket=? WHERE id=?", (pane["socket"], run_id))
        store.update_run(
            run_id,
            status="running",
            started_at=now(),
            session_id=pane["session_id"],
            window_id=pane["window_id"],
            pane_id=pane_id,
        )
    code = 1
    error = None
    child = None
    handlers = {}

    def forward(signum, frame):
        if child and child.poll() is None:
            child.send_signal(signum)

    try:
        for sig in (signal.SIGTERM, signal.SIGHUP):
            handlers[sig] = signal.signal(sig, forward)
        # The foreground agent receives terminal SIGINT itself.
        handlers[signal.SIGINT] = signal.signal(signal.SIGINT, lambda *_: None)
        env = os.environ.copy()
        env["WTR_LINEAR_WORKSPACE"] = run["workspace_slug"]
        env["WTR_RUN_ID"] = run_id
        child = subprocess.Popen(json.loads(run["argv"]), cwd=run["cwd"], env=env)
        code = child.wait()
    except OSError as exc:
        error = str(exc)
    finally:
        for sig, handler in handlers.items():
            signal.signal(sig, handler)
        store.update_run(
            run_id,
            status="awaiting_review" if code == 0 else "failed",
            ended_at=now(),
            exit_code=code,
            error=error,
        )
    print(f"\n[wtr] agent 已退出，exit={code}；已回到 worktree 的 shell。", flush=True)
    return code if code >= 0 else 128 - code
