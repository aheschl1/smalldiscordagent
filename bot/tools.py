"""Tool schemas + implementations. Descriptions are deliberately terse (they're sent on every call).

Read tools go to everyone with access; write tools are only *offered* to users with write
permission, so the permission boundary is enforced by capability, not by prompt.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from . import github as gh
from . import linear
from .gitrepo import GitError, Repo, Worktree, git, safe_path, snapshot

log = logging.getLogger("tools")


@dataclass
class ToolCtx:
    repos: dict[str, Repo]
    default_repo: str
    worktrees: dict[str, Worktree]  # this thread's write sessions, keyed by repo
    pinned: dict[str, str]          # repo -> default-branch sha pinned for this request
    max_out: int
    requester: str
    draft_prs: bool
    origin: str = ""  # link back to where the request came from (Discord thread URL)
    level: str = "read"
    user_id: int = 0
    # Asks the requesting user to approve a dangerous action (Discord button). None = can't ask, so deny.
    confirm: Callable[[str], Awaitable[bool]] | None = None


Impl = Callable[[dict, ToolCtx], Awaitable[str]]
_READ: list[tuple[dict, Impl]] = []
_WRITE: list[tuple[dict, Impl]] = []

REPO = {"repo": {"type": "string", "description": "owner/name; omit for default"}}
REF = {"ref": {"type": "string", "description": "branch, tag, sha, or pr/N; omit for default branch"}}
BASE = {"base": {"type": "string", "description": "branch to start from and target with the PR; first edit only; "
                                                  "default: default branch"}}


def tool(registry: list, name: str, desc: str, props: dict, required: tuple[str, ...] = ()):
    # Responses API function tool; strict=False so optional params stay optional.
    schema = {"type": "function", "name": name, "description": desc, "strict": False,
              "parameters": {"type": "object", "properties": props, "required": list(required)}}

    def deco(fn: Impl) -> Impl:
        registry.append((schema, fn))
        return fn
    return deco


def cap(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n] + f"\n…[truncated {len(s) - n} chars; narrow the request]"


def clip(line: str, n: int = 300) -> str:
    return line if len(line) <= n else line[:n] + "…"


def repo_of(c: ToolCtx, a: dict) -> Repo:
    name = a.get("repo") or c.default_repo
    if name not in c.repos:
        raise ValueError(f"repo not allowed: {name}. Allowed: {', '.join(c.repos)}")
    return c.repos[name]


async def tree_of(c: ToolCtx, a: dict) -> tuple[Repo, str]:
    """Tree-ish to read: explicit ref > this thread's worktree (edits visible) > pinned default branch."""
    r = repo_of(c, a)
    if a.get("ref"):
        return r, await r.resolve(a["ref"])
    if wt := c.worktrees.get(r.name):
        return r, wt.tree
    if r.name not in c.pinned:
        c.pinned[r.name] = await r.resolve()
    return r, c.pinned[r.name]


def pathspec(p: str | None) -> str | None:
    if not p:
        return None
    p = p.lstrip("/").removeprefix("./")
    return f":(glob){p}" if "*" in p else p


def compact_tree(files: list[str], prefix: str, depth: int, limit: int = 300) -> str:
    """Files up to `depth` levels; deeper directories collapsed to 'dir/ (N)'."""
    pre = prefix.strip("/") + "/" if prefix.strip("/") else ""
    out: dict[str, int] = {}
    for f in files:
        if not f.startswith(pre):
            continue
        parts = f[len(pre):].split("/")
        key = "/".join(parts) if len(parts) <= depth else "/".join(parts[:depth]) + "/"
        out[key] = out.get(key, 0) + 1
    if not out:
        return f"no files under {prefix or '/'}"
    lines = [f"{k} ({n})" if k.endswith("/") else k for k, n in out.items()]
    more = f"\n…{len(lines) - limit} more; narrow path" if len(lines) > limit else ""
    return "\n".join(lines[:limit]) + more


# ---------------------------------------------------------------- read tools

@tool(_READ, "ls", "List files. Deeper dirs collapse to 'dir/ (count)'.",
      {**REPO, "path": {"type": "string"}, "depth": {"type": "integer", "description": "default 2"}, **REF})
async def _ls(a, c):
    r, t = await tree_of(c, a)
    return compact_tree(await r.list_files(t), a.get("path") or "", int(a.get("depth") or 2))


@tool(_READ, "read", "Read a file with line numbers. Default: 250 lines from start.",
      {**REPO, "path": {"type": "string"}, "start": {"type": "integer"}, "end": {"type": "integer"}, **REF},
      ("path",))
async def _read(a, c):
    r, t = await tree_of(c, a)
    lines = (await r.show(t, a["path"].lstrip("/").removeprefix("./"))).split("\n")
    s = max(1, int(a.get("start") or 1))
    e = min(len(lines), int(a.get("end") or s + 249))
    body = "\n".join(f"{s + i}\t{clip(l, 400)}" for i, l in enumerate(lines[s - 1:e]))
    return body + (f"\n[lines {s}-{e} of {len(lines)}]" if e < len(lines) or s > 1 else "")


@tool(_READ, "grep", "Regex (ERE) search. Returns path:line:text.",
      {**REPO, "pattern": {"type": "string"},
       "path": {"type": "string", "description": "dir, file, or glob like src/**/*.py"},
       "ignore_case": {"type": "boolean"}, **REF},
      ("pattern",))
async def _grep(a, c):
    r, t = await tree_of(c, a)
    hits = await r.grep(t, a["pattern"], pathspec(a.get("path")), bool(a.get("ignore_case")))
    if not hits:
        return "no matches"
    limit = 60
    out = "\n".join(clip(h, 240) for h in hits[:limit])
    if len(hits) > limit:
        files = len({h.split(":", 1)[0] for h in hits})
        out += f"\n…{len(hits) - limit} more matches across {files} files; narrow pattern/path"
    return out


@tool(_READ, "log", "Commit history. `search` finds commits that added/removed that string (when did X change).",
      {**REPO, "path": {"type": "string"}, "search": {"type": "string"},
       "n": {"type": "integer", "description": "default 15"}, **REF})
async def _log(a, c):
    r = repo_of(c, a)
    out = await r.log(await r.resolve(a.get("ref")), min(int(a.get("n") or 15), 50), a.get("path"), a.get("search"))
    return out or "no commits"


@tool(_READ, "pr", "Pull request: metadata, changed files, patches. `file` gets one file's full patch. "
      "Read PR code with ref=pr/N.",
      {**REPO, "number": {"type": "integer"}, "file": {"type": "string"}}, ("number",))
async def _pr(a, c):
    r = repo_of(c, a)
    n = int(a["number"])
    p, files = await asyncio.gather(gh.get_pr(r.name, n), gh.get_pr_files(r.name, n))
    if f := a.get("file"):
        hit = next((x for x in files if x["filename"] == f), None) or next((x for x in files if f in x["filename"]), None)
        if not hit:
            return "no such file in PR. Files: " + ", ".join(x["filename"] for x in files)
        return cap(f"{hit['filename']}\n{hit.get('patch') or '(no textual patch)'}", c.max_out)
    state = "merged" if p.get("merged") else p["state"]
    out = (f"#{p['number']} \"{p['title']}\" by {p['user']['login']} [{state}{', draft' if p.get('draft') else ''}] "
           f"{p['base']['ref']} <- {p['head']['ref']} ({p['head']['sha'][:8]}), +{p['additions']} -{p['deletions']}\n"
           f"{cap(p.get('body') or '', 1500)}\n\nfiles:\n"
           + "\n".join(f"{x['status'][0].upper()} {x['filename']} +{x['additions']} -{x['deletions']}" for x in files)
           + "\n")
    omitted = []
    for x in files:
        chunk = f"\n--- {x['filename']}\n{x.get('patch') or '(binary/large)'}\n"
        if len(out) + len(chunk) > c.max_out:
            omitted.append(x["filename"])
        else:
            out += chunk
    if omitted:
        out += "\n[patches omitted, fetch with file=: " + ", ".join(omitted) + "]"
    return out


_TS = re.compile(r"^\d{4}-\d\d-\d\dT[\d:.]+Z\s?")
_ERR = re.compile(r"error|fail|exception|panic|traceback|assert", re.IGNORECASE)


@tool(_READ, "ci", "CI check runs for a ref (default branch or pr/N). Pass `job` (a check id) for its failure log.",
      {**REPO, **REF, "job": {"type": "integer"}})
async def _ci(a, c):
    r = repo_of(c, a)
    if job := a.get("job"):
        lines = [_TS.sub("", l) for l in (await gh.get_job_log(r.name, int(job))).splitlines()]
        picked: set[int] = set()
        for i in [i for i, l in enumerate(lines) if _ERR.search(l)][:15]:
            picked.update(range(max(0, i - 2), min(len(lines), i + 3)))
        excerpt = "\n".join(clip(lines[i]) for i in sorted(picked)) or "(none matched)"
        tail = "\n".join(clip(l) for l in lines[-30:])
        return cap(f"== error lines ==\n{excerpt}\n== tail ==\n{tail}", c.max_out)
    sha = await r.resolve(a.get("ref"))
    runs = await gh.get_check_runs(r.name, sha)
    if not runs:
        return f"no checks on {sha[:8]}"
    return "\n".join(f"{x['id']} {x['name']}: {x.get('conclusion') or x['status']}" for x in runs)


# ---------------------------------------------------------------- write tools

async def worktree_for(c: ToolCtx, a: dict) -> Worktree:
    r = repo_of(c, a)
    if r.name not in c.worktrees:
        branch = (a.get("base") or "").strip()
        if branch and branch != r.default_branch:
            sha = await r.resolve(branch)
        else:
            branch, sha = "", c.pinned.get(r.name) or await r.resolve()
        c.worktrees[r.name] = await r.add_worktree(sha, branch)
    return c.worktrees[r.name]


@tool(_WRITE, "edit", "Replace an exact, unique string in a file. Include enough context to be unique.",
      {**REPO, "path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"}, **BASE},
      ("path", "old", "new"))
async def _edit(a, c):
    wt = await worktree_for(c, a)
    p = safe_path(wt.dir, a["path"])
    if not p.is_file():
        return f"error: {a['path']} does not exist (use write to create)"
    src = p.read_text()
    n = src.count(a["old"])
    if n != 1:
        return f"error: old string found {n} times in {a['path']}; must be exactly 1"
    p.write_text(src.replace(a["old"], a["new"], 1))
    await snapshot(wt)
    return f"ok (line {src[:src.index(a['old'])].count(chr(10)) + 1})"


@tool(_WRITE, "write", "Create or overwrite a file with full content.",
      {**REPO, "path": {"type": "string"}, "content": {"type": "string"}, **BASE}, ("path", "content"))
async def _write(a, c):
    wt = await worktree_for(c, a)
    p = safe_path(wt.dir, a["path"])
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(a["content"])
    await snapshot(wt)
    return "ok"


@tool(_WRITE, "open_pr", "Commit your edits and open a PR (or push to this thread's existing PR).",
      {**REPO, "title": {"type": "string"}, "body": {"type": "string"}}, ("title", "body"))
async def _open_pr(a, c):
    r = repo_of(c, a)
    wt = c.worktrees.get(r.name)
    if not wt:
        return "error: no edits made yet"
    await git("add", "-A", cwd=wt.dir)
    stat = await git("diff", "--cached", "--stat", cwd=wt.dir)
    if not stat.strip():
        return f"no new changes; PR is {wt.pr[1]}" if wt.pr else "error: no changes to commit"
    await git("commit", "-q", "-m", a["title"], cwd=wt.dir)
    await r.push(wt)
    if wt.pr:
        return f"pushed to existing PR {wt.pr[1]}\n{stat}"
    body = f"{a['body']}\n\n---\n_Requested by {c.requester} via Discord._"
    pr = await gh.create_pr(r.name, a["title"], body, wt.branch, wt.base or r.default_branch, c.draft_prs)
    wt.pr = (pr["number"], pr["html_url"])
    return f"opened {pr['html_url']}\n{stat}"


@tool(_WRITE, "review", "Post your review as a comment on a GitHub PR. Only when the user asks to post it.",
      {**REPO, "number": {"type": "integer"}, "body": {"type": "string"}}, ("number", "body"))
async def _review(a, c):
    return (await gh.post_review(repo_of(c, a).name, int(a["number"]), a["body"]))["html_url"]


# ---------------------------------------------------------------- registry

def toolset(level: str, repos: list[str] | None = None) -> tuple[list[dict], dict[str, Impl]]:
    """Tools offered at a permission level. The model never sees tools above the user's level."""
    from . import apis, memory  # late import: both modules use helpers from here
    write = level == "write"
    items = (_READ + _WRITE if write else _READ) + linear.tools(write) + apis.tools(repos or [])
    if write:
        items += memory.tools()
    return [s for s, _ in items], {s["name"]: f for s, f in items}


async def run_tool(impls: dict[str, Impl], name: str, args: dict, ctx: ToolCtx) -> str:
    fn = impls.get(name)
    if not fn:
        return f"error: tool {name} not available"
    try:
        return cap(await fn(args, ctx), ctx.max_out)
    except (GitError, gh.GitHubError, linear.LinearError, ValueError, KeyError, OSError) as e:
        return f"error: {e}"
    except Exception as e:  # never let one tool failure kill the whole turn
        log.exception("tool %s failed", name)
        return f"error: {type(e).__name__}: {e}"


def describe(name: str, args: dict) -> str:
    """Short human label for progress updates."""
    key = next((args[k] for k in ("pattern", "path", "number", "title", "search", "query", "issue", "job", "ref",
                                  "channel") if args.get(k)), "")
    return f"{name} {str(key)[:60]}".strip()

