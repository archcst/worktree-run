"""Repository binding identities, independent of project prompt templates."""

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
        name=target["name"],
        team_name=f"{team['name']} ({team['key']})" if team else "",
    )


def describe(row):
    scope = f" · {row['team_name'] or 'Workspace'}" if row["kind"] == "label" else ""
    return (
        f"{KINDS[row['kind']]}: {row['name']}{scope} · {row['workspace_slug']} [{row['target_id']}]"
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
