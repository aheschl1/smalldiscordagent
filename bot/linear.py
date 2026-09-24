"""Linear tools (GraphQL API). Offered only when LINEAR_API_KEY is set.

Anyone with read access can search, create and comment; changing an existing issue's
status/assignee/priority/title requires write.
"""

from __future__ import annotations

import os
import re
import time

import httpx

API = "https://api.linear.app/graphql"
KEY = os.environ.get("LINEAR_API_KEY", "")
DEFAULT_TEAM = os.environ.get("LINEAR_TEAM", "")  # team key, e.g. "ENG"; optional if there's only one team

_client = httpx.AsyncClient(timeout=30, headers={"Authorization": KEY, "Content-Type": "application/json"})
_IDENT = re.compile(r"^[A-Za-z][A-Za-z0-9]*-\d+$")
PRIORITIES = {"none": 0, "urgent": 1, "high": 2, "medium": 3, "normal": 3, "low": 4}
PRIORITY_NAMES = {0: "none", 1: "urgent", 2: "high", 3: "medium", 4: "low"}


class LinearError(Exception):
    pass


def enabled() -> bool:
    return bool(KEY)


async def gql(query: str, **variables) -> dict:
    r = await _client.post(API, json={"query": query, "variables": variables})
    data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.is_error or data.get("errors"):
        msg = "; ".join(e.get("message", "") for e in data.get("errors", [])) or r.text[:300]
        raise LinearError(f"Linear: {msg}")
    return data["data"]


# ---------------------------------------------------------------- name -> id lookups (cached 10 min)

_meta: dict = {}
_meta_at = 0.0


async def meta() -> dict:
    global _meta, _meta_at
    if time.monotonic() - _meta_at < 600 and _meta:
        return _meta
    d = await gql("""{
      teams(first: 50) { nodes { id key name
        states(first: 50) { nodes { id name type } }
        labels(first: 100) { nodes { id name } } } }
      users(first: 250) { nodes { id name displayName active } }
    }""")
    _meta = {"teams": d["teams"]["nodes"], "users": [u for u in d["users"]["nodes"] if u["active"]]}
    _meta_at = time.monotonic()
    return _meta


def _find(items: list[dict], name: str, *fields: str) -> dict | None:
    n = name.strip().lower().lstrip("@")
    for f in fields:  # exact match first, then prefix
        if hit := next((x for x in items if (x.get(f) or "").lower() == n), None):
            return hit
    for f in fields:
        if hit := next((x for x in items if (x.get(f) or "").lower().startswith(n)), None):
            return hit
    return None


async def _team(name: str | None) -> dict:
    m = await meta()
    name = name or DEFAULT_TEAM
    if not name:
        if len(m["teams"]) == 1:
            return m["teams"][0]
        raise LinearError("which team? " + ", ".join(f"{t['key']} ({t['name']})" for t in m["teams"]))
    t = _find(m["teams"], name, "key", "name")
    if not t:
        raise LinearError(f"no team {name!r}. Teams: " + ", ".join(t["key"] for t in m["teams"]))
    return t


async def _user(name: str) -> dict:
    u = _find((await meta())["users"], name, "displayName", "name")
    if not u:
        raise LinearError(f"no Linear user {name!r}")
    return u


def _priority(p) -> int:
    if isinstance(p, int) or str(p).isdigit():
        return int(p)
    if str(p).lower() not in PRIORITIES:
        raise LinearError("priority must be urgent/high/medium/low/none")
    return PRIORITIES[str(p).lower()]


# ---------------------------------------------------------------- tools

ISSUE_FIELDS = "identifier title url priority state { name } assignee { displayName } team { key } updatedAt"


def _line(i: dict) -> str:
    who = (i.get("assignee") or {}).get("displayName") or "unassigned"
    return (f"{i['identifier']} [{i['state']['name']}, {PRIORITY_NAMES.get(i['priority'], '?')}, {who}] "
            f"{i['title']} ({i['updatedAt'][:10]})")


async def search(a: dict, c) -> str:
    q = (a.get("query") or "").strip()
    if _IDENT.match(q):  # a specific issue: full details + recent comments
        d = await gql(f"""query($id: String!) {{ issue(id: $id) {{ {ISSUE_FIELDS} description
            labels {{ nodes {{ name }} }}
            comments(first: 10) {{ nodes {{ body createdAt user {{ displayName }} }} }} }} }}""", id=q.upper())
        i = d["issue"]
        labels = ", ".join(x["name"] for x in i["labels"]["nodes"])
        comments = "\n".join(f"- {(x.get('user') or {}).get('displayName', 'bot')} ({x['createdAt'][:10]}): "
                             f"{x['body'][:400]}" for x in i["comments"]["nodes"])
        return (f"{_line(i)}\n{i['url']}\nlabels: {labels or '-'}\n\n{(i.get('description') or '')[:3000]}"
                + (f"\n\ncomments:\n{comments}" if comments else ""))
    filt: dict = {}
    if a.get("state"):
        filt["state"] = {"name": {"eqIgnoreCase": a["state"]}}
    if a.get("team"):
        filt["team"] = {"id": {"eq": (await _team(a["team"]))["id"]}}
    n = max(1, min(int(a.get("limit") or 10), 25))
    if q:
        d = await gql(f"""query($q: String!, $n: Int, $f: IssueFilter) {{
            searchIssues(term: $q, first: $n, filter: $f) {{ nodes {{ {ISSUE_FIELDS} }} }} }}""", q=q, n=n, f=filt)
        nodes = d["searchIssues"]["nodes"]
    else:
        d = await gql(f"""query($n: Int, $f: IssueFilter) {{
            issues(first: $n, filter: $f, orderBy: updatedAt) {{ nodes {{ {ISSUE_FIELDS} }} }} }}""", n=n, f=filt)
        nodes = d["issues"]["nodes"]
    return "\n".join(_line(i) for i in nodes) or "no issues found"


async def create(a: dict, c) -> str:
    team = await _team(a.get("team"))
    footer = f"\n\n---\n_Filed by {c.requester} via Discord" + (f" ([thread]({c.origin}))" if c.origin else "") + "._"
    inp: dict = {"teamId": team["id"], "title": a["title"], "description": (a.get("description") or "") + footer}
    if a.get("priority") is not None:
        inp["priority"] = _priority(a["priority"])
    if a.get("labels"):
        ids = []
        for name in a["labels"]:
            lab = _find(team["labels"]["nodes"], name, "name")
            if not lab:
                return f"error: no label {name!r} in {team['key']}. Labels: " + \
                    ", ".join(x["name"] for x in team["labels"]["nodes"])
            ids.append(lab["id"])
        inp["labelIds"] = ids
    if a.get("assignee"):
        inp["assigneeId"] = (await _user(a["assignee"]))["id"]
    d = await gql("""mutation($i: IssueCreateInput!) { issueCreate(input: $i) {
        success issue { identifier url } } }""", i=inp)
    i = d["issueCreate"]["issue"]
    return f"created {i['identifier']} {i['url']}"


async def comment(a: dict, c) -> str:
    body = f"{a['body']}\n\n_— {c.requester} via Discord_"
    d = await gql("""mutation($i: CommentCreateInput!) { commentCreate(input: $i) { success comment { url } } }""",
                  i={"issueId": a["issue"].upper(), "body": body})
    return f"commented {d['commentCreate']['comment']['url']}"


async def update(a: dict, c) -> str:
    ident = a["issue"].upper()
    inp: dict = {}
    if a.get("title"):
        inp["title"] = a["title"]
    if a.get("priority") is not None:
        inp["priority"] = _priority(a["priority"])
    if a.get("assignee"):
        inp["assigneeId"] = None if a["assignee"].lower() in ("none", "unassigned") else (await _user(a["assignee"]))["id"]
    if a.get("state"):
        team_key = ident.split("-")[0]
        team = await _team(team_key)
        st = _find(team["states"]["nodes"], a["state"], "name")
        if not st:
            return f"error: no state {a['state']!r}. States: " + ", ".join(x["name"] for x in team["states"]["nodes"])
        inp["stateId"] = st["id"]
    if not inp:
        return "error: nothing to update"
    d = await gql("""mutation($id: String!, $i: IssueUpdateInput!) { issueUpdate(id: $id, input: $i) {
        success issue { identifier url state { name } } } }""", id=ident, i=inp)
    i = d["issueUpdate"]["issue"]
    return f"updated {i['identifier']} ({i['state']['name']}) {i['url']}"


def _schema(name: str, desc: str, props: dict, required: tuple[str, ...] = ()) -> dict:
    return {"type": "function", "name": name, "description": desc, "strict": False,
            "parameters": {"type": "object", "properties": props, "required": list(required)}}


S = {"type": "string"}
SEARCH = _schema("linear_search", "Search Linear issues (text), list recent ones (no query), or get one issue with "
                 "comments (query = identifier like ENG-12).",
                 {"query": S, "state": S, "team": S, "limit": {"type": "integer"}})
CREATE = _schema("linear_create", "Create a Linear issue. Search first to avoid duplicates. Only when asked.",
                 {"title": S, "description": {"type": "string", "description": "markdown"}, "team": S,
                  "priority": {"type": "string", "description": "urgent|high|medium|low"},
                  "labels": {"type": "array", "items": S}, "assignee": S}, ("title",))
COMMENT = _schema("linear_comment", "Comment on a Linear issue. Only when asked.",
                  {"issue": {"type": "string", "description": "identifier, e.g. ENG-12"}, "body": S},
                  ("issue", "body"))
UPDATE = _schema("linear_update", "Change a Linear issue's state, assignee, priority or title. Only when asked.",
                 {"issue": S, "state": S, "assignee": S, "priority": S, "title": S}, ("issue",))


def tools(write: bool) -> list[tuple[dict, object]]:
    if not enabled():
        return []
    out = [(SEARCH, search), (CREATE, create), (COMMENT, comment)]
    if write:
        out.append((UPDATE, update))
    return out
