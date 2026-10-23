"""Linear matching conditions for stable, local WTR projects."""

import uuid

KINDS = {"project": "Project", "label": "Label"}


def key(row):
    return row["workspace"], row["kind"], row["target_id"]


def target_text(kind, target):
    scope = ""
    if kind == "label":
        team = target.get("team")
        scope = f" · {team['name']} ({team['key']})" if team else " · Workspace"
    return f"{target['name']}{scope} [{target['id']}]"


def from_target(linear, kind, target, values):
    team = target.get("team")
    return dict(
        values,
        workspace=linear.workspace,
        workspace_slug=linear.slug,
        kind=kind,
        target_id=target["id"],
        target_name=target["name"],
        name=values.get("name", target["name"]),
        project_id=values.get("project_id") or str(uuid.uuid4()),
        agent=values.get("agent"),
        tmux_session=values.get("tmux_session"),
        enabled=values.get("enabled", 1),
        team_name=f"{team['name']} ({team['key']})" if team else "",
    )


def describe(row):
    scope = f" · {row['team_name'] or 'Workspace'}" if row["kind"] == "label" else ""
    return (
        f"{row['name']} · {KINDS[row['kind']]}: {row['target_name']}{scope} "
        f"· {row['workspace_slug']} [{row['project_id']}]"
        + (" · 已停用匹配" if not row["enabled"] else "")
    )


def matching(store, workspace, issue):
    targets = {("label", label["id"]) for label in issue["labels"]["nodes"]}
    if issue.get("project"):
        targets.add(("project", issue["project"]["id"]))
    return [
        row
        for row in store.bindings()
        if row["workspace"] == workspace and (row["kind"], row["target_id"]) in targets
    ]
