"""General GitHub REST and Linear GraphQL tools.

Two small schemas give the model the whole API instead of dozens of narrow tools. Access is decided
per call, not per tool:
  read   GET requests / GraphQL queries                      -> anyone with access
  write  POST/PATCH/PUT / GraphQL mutations                  -> write users
  danger merge, close, delete, settings, secrets, releases…  -> write users + a Confirm button click
GitHub calls are confined to the configured repos.
"""

from __future__ import annotations

import json
import re

from . import github as gh
from . import linear

# ---------------------------------------------------------------- classification

_GH_DANGER_PATH = re.compile(
    r"/(pulls/\d+/merge|merges|collaborators|invitations|hooks|keys|secrets|variables|environments|rulesets"
    r"|branches/[^/]+/protection|actions/permissions|transfer|releases|git/refs|actions/workflows/[^/]+/dispatches"
    r"|dispatches)(/|$)")
_LINEAR_DANGER = re.compile(
    r"\b\w*(Delete|Archive|Remove|Trash|Merge)\s*\(|\b(team|organization|workflowState|webhook|apiKey|integration"
    r"|user|oauth\w*)(Create|Update|Delete)\s*\(", re.IGNORECASE)


def classify_github(method: str, path: str, body: dict | None) -> str:
    if method == "GET":
        return "read"
    if method == "DELETE" or _GH_DANGER_PATH.search(path):
        return "danger"
    if re.fullmatch(r"/repos/[^/]+/[^/]+", path):  # repo settings (rename, visibility, default branch…)
        return "danger"
    body = body or {}
    if body.get("state") == "closed":  # closing issues/PRs
        return "danger"
    if re.search(r"/pulls/\d+/reviews$", path) and body.get("event") in ("APPROVE", "REQUEST_CHANGES"):
        return "danger"
    return "write"


def classify_linear(query: str) -> str:
    q = re.sub(r"#[^\n]*", "", query)
    if not re.search(r"\bmutation\b", q):
        return "read"
    return "danger" if _LINEAR_DANGER.search(q) else "write"


async def gate(kind: str, summary: str, c) -> str | None:
    """None if allowed; otherwise an error message for the model."""
    if kind == "read":
        return None
    if c.level != "write":
        return "error: this user has read-only access; they need write access for that"
    if kind == "danger":
        if not c.confirm:
            return "error: this action needs confirmation, which isn't available here"
        if not await c.confirm(summary):
            return "cancelled: the user did not confirm this action. Don't retry unless they ask again."
    return None


# ---------------------------------------------------------------- output compaction

_DROP = re.compile(r"(^|_)url$|^node_id$|^gravatar_id$|^_links$|^avatar_url$|^performed_via_github_app$|^reactions$")


def compact(v, depth: int = 0):
    """Strip API noise (url fields, node ids, nested user objects) so results cost fewer tokens."""
    if isinstance(v, dict):
        if "login" in v and "type" in v and depth > 0:  # a user/org object -> just the login
            return v["login"]
        return {k: compact(x, depth + 1) for k, x in v.items()
                if k == "html_url" or not _DROP.search(k)}
    if isinstance(v, list):
        return [compact(x, depth + 1) for x in v]
    return v


# ---------------------------------------------------------------- tools

S = {"type": "string"}


def _schema(name, desc, props, required=()):
    return {"type": "function", "name": name, "description": desc, "strict": False,
            "parameters": {"type": "object", "properties": props, "required": list(required)}}


def github_tool(repos: list[str]):
    schema = _schema(
        "github_api",
        "Call any GitHub REST endpoint for the allowed repos (issues, labels, comments, merges, branches, "
        "releases, workflow runs, settings…). Path like /repos/owner/name/issues. Prefer the specific tools "
        "(pr, ci, open_pr) when they fit. Destructive calls ask the user to confirm.",
        {"method": {"type": "string", "enum": ["GET", "POST", "PATCH", "PUT", "DELETE"]},
         "path": S, "body": {"type": "object"}, "why": {"type": "string", "description": "one line, shown on confirm"}},
        ("method", "path"))
    allowed = tuple(f"/repos/{r}".lower() for r in repos)

    async def impl(a: dict, c) -> str:
        method = a["method"].upper()
        path = "/" + re.sub(r"^https://api\.github\.com", "", a["path"]).lstrip("/")
        base = path.split("?")[0].lower()
        if not (any(base == p or base.startswith(p + "/") for p in allowed)
                or (method == "GET" and base.startswith("/search/"))):
            return f"error: path must be under /repos/<repo> for: {', '.join(repos)}"
        body = a.get("body")
        kind = classify_github(method, base, body)
        summary = f"GitHub `{method} {path}`" + (f"\n```json\n{json.dumps(body)[:800]}\n```" if body else "") + \
                  (f"\n{a['why']}" if a.get("why") else "")
        if err := await gate(kind, summary, c):
            return err
        r = await gh._client.request(method, path, json=body if body is not None else None)
        if r.status_code == 204:
            return f"{r.status_code} ok"
        try:
            data = compact(r.json())
        except ValueError:
            data = r.text
        out = json.dumps(data, separators=(",", ":")) if not isinstance(data, str) else data
        return f"{r.status_code} {out}"

    return schema, impl


LINEAR_SCHEMA = _schema(
    "linear_graphql",
    "Run any Linear GraphQL query or mutation (projects, cycles, labels, bulk updates…). Prefer linear_search/"
    "linear_create/linear_comment when they fit. Destructive mutations ask the user to confirm.",
    {"query": S, "variables": {"type": "object"}, "why": {"type": "string", "description": "one line, shown on confirm"}},
    ("query",))


async def linear_impl(a: dict, c) -> str:
    kind = classify_linear(a["query"])
    summary = f"Linear mutation\n```graphql\n{a['query'][:900]}\n```" + \
              (f"\nvariables: `{json.dumps(a.get('variables'))[:300]}`" if a.get("variables") else "") + \
              (f"\n{a['why']}" if a.get("why") else "")
    if err := await gate(kind, summary, c):
        return err
    data = await linear.gql(a["query"], **(a.get("variables") or {}))
    return json.dumps(data, separators=(",", ":"))


def tools(repos: list[str]) -> list:
    out = [github_tool(repos)]
    if linear.enabled():
        out.append((LINEAR_SCHEMA, linear_impl))
    return out
