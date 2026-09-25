"""Linear CLI 2.3+ adapter; only structured GraphQL responses are parsed."""

import json
import re

from .util import Error, execute, require

TERMINAL_STATE_TYPES = ("completed", "canceled", "duplicate")

ISSUE_FIELDS = """id identifier title description priority createdAt
    project { id name } state { name type } assignee { id }"""


class Linear:
    def __init__(self, workspace=None):
        require("linear")
        self.slug = workspace
        context = self.query("query { viewer { id } organization { id name urlKey } }")
        try:
            self.workspace = context["organization"]["id"]
            self.slug = context["organization"]["urlKey"]
            self.user = context["viewer"]["id"]
        except (KeyError, TypeError) as exc:
            raise Error("Linear 未返回有效用户和 workspace") from exc
        if not all(isinstance(v, str) and v for v in (self.workspace, self.slug, self.user)):
            raise Error("Linear workspace 信息为空")
        # Validate the slug as a credential selector, not merely a display label.
        verified = self.query("query { organization { id } }")
        if verified.get("organization", {}).get("id") != self.workspace:
            raise Error("Linear workspace 凭据与组织身份不匹配")

    def query(self, query, variables=None):
        args = ["linear", "api", query, "--variables-json", json.dumps(variables or {})]
        if self.slug:
            args += ["--workspace", self.slug]
        output = execute(args).stdout
        try:
            result = json.loads(output)
        except ValueError as exc:
            raise Error("Linear 未返回 JSON；需要支持 linear api 的 CLI（已验证 2.3.0）") from exc
        if (
            not isinstance(result, dict)
            or result.get("errors")
            or not isinstance(result.get("data"), dict)
        ):
            raise Error(f"Linear GraphQL 错误：{result}")
        return result["data"]

    def pages(self, query, field, variables=None):
        cursor = None
        seen = set()
        records = []
        while True:
            data = self.query(query, {**(variables or {}), "after": cursor})
            try:
                connection = data[field]
                nodes = connection["nodes"]
                info = connection["pageInfo"]
                if not isinstance(nodes, list):
                    raise TypeError()
                records.extend(nodes)
                if not info["hasNextPage"]:
                    return records
                cursor = info["endCursor"]
                if not cursor or cursor in seen:
                    raise Error("Linear 返回重复或缺失的分页游标")
                seen.add(cursor)
            except (KeyError, TypeError) as exc:
                raise Error("Linear 分页响应结构不正确") from exc

    def projects(self):
        projects = self.pages(
            """query($after: String) {
            projects(first: 100, after: $after) {
              nodes { id name } pageInfo { hasNextPage endCursor }
            }}""",
            "projects",
        )
        if any(
            not isinstance(p, dict)
            or not all(isinstance(p.get(k), str) and p[k] for k in ("id", "name"))
            for p in projects
        ):
            raise Error("Linear 项目响应缺少有效 ID/名称")
        return projects

    def mine(self):
        issues = self.pages(
            """query($after: String, $closedTypes: [String!]!) {
            issues(first: 100, after: $after, filter: {
              assignee: {isMe: {eq: true}}, state: {type: {nin: $closedTypes}}
            }) { nodes { """
            + ISSUE_FIELDS
            + """ }
            pageInfo { hasNextPage endCursor } }}""",
            "issues",
            {"closedTypes": TERMINAL_STATE_TYPES},
        )
        issues = [self.validate(i) for i in issues]
        return sorted(
            (i for i in issues if self.is_mine_open(i)),
            key=lambda i: (i.get("priority") or 5, i.get("createdAt", "")),
        )

    def is_mine_open(self, issue):
        return (issue.get("assignee") or {}).get("id") == self.user and issue["state"][
            "type"
        ] not in TERMINAL_STATE_TYPES

    def issue(self, identifier):
        data = self.query(
            "query($id: String!) { issue(id: $id) { " + ISSUE_FIELDS + " } }", {"id": identifier}
        )
        return self.validate(data.get("issue"))

    def validate(self, issue):
        if (
            not isinstance(issue, dict)
            or not isinstance(issue.get("id"), str)
            or not issue["id"]
            or not isinstance(issue.get("identifier"), str)
            or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*-\d+", issue["identifier"])
        ):
            raise Error("Linear 未返回有效的 issue ID/identifier")
        if (
            not isinstance(issue.get("title"), str)
            or not isinstance(issue.get("state"), dict)
            or not all(isinstance(issue["state"].get(k), str) for k in ("name", "type"))
            or not isinstance(issue.get("priority"), int)
            or not 0 <= issue["priority"] <= 4
            or not isinstance(issue.get("createdAt"), str)
            or (issue.get("description") is not None and not isinstance(issue["description"], str))
        ):
            raise Error("Linear issue 响应字段结构不正确")
        for field in ("project", "assignee"):
            value = issue.get(field)
            if value is not None and (
                not isinstance(value, dict) or not isinstance(value.get("id"), str)
            ):
                raise Error(f"Linear issue 的 {field} 字段无效")
        return issue
