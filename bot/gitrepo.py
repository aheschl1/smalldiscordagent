"""Git plumbing.

Each repo is a bare clone; reads go straight to the object DB at a tree-ish (no checkout, so
concurrent requests are safe). Writes happen in a per-thread worktree; after each edit we snapshot
the index with `git write-tree`, so the same read code sees the bot's own edits.
"""

from __future__ import annotations

import asyncio
import base64
import os
import posixpath
import re
import secrets
import shutil
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path

TOKEN = os.environ.get("GITHUB_TOKEN", "")
AUTHOR = os.environ.get("GIT_AUTHOR_NAME", "repo-agent-bot")
EMAIL = os.environ.get("GIT_AUTHOR_EMAIL", "repo-agent-bot@users.noreply.github.com")

# Token is injected via env-based git config: never written to .git/config, never in argv.
GIT_ENV = {
    **os.environ,
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_AUTHOR_NAME": AUTHOR, "GIT_AUTHOR_EMAIL": EMAIL,
    "GIT_COMMITTER_NAME": AUTHOR, "GIT_COMMITTER_EMAIL": EMAIL,
}
if TOKEN:
    GIT_ENV |= {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
        "GIT_CONFIG_VALUE_0": "AUTHORIZATION: basic " + base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode(),
    }


class GitError(Exception):
    pass


async def git(*args: str, cwd: Path | str | None = None, ok: tuple[int, ...] = (0,)) -> str:
    p = await asyncio.create_subprocess_exec(
        "git", *args, cwd=cwd, env=GIT_ENV,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await p.communicate()
    if p.returncode not in ok:
        raise GitError(f"git {args[0]}: {(err or out).decode(errors='replace').strip()[:500]}")
    return out.decode(errors="replace")


@dataclass
class Worktree:
    dir: Path
    branch: str
    tree: str                       # snapshot of the index after the last edit; used for reads
    pr: tuple[int, str] | None = None  # (number, url)


class Repo:
    def __init__(self, name: str, data_dir: Path):
        self.name = name
        self.slug = name.replace("/", "__")
        self.data_dir = data_dir
        self.git_dir = data_dir / "repos" / f"{self.slug}.git"
        self.default_branch = "main"
        self.lock = asyncio.Lock()  # serializes fetch / worktree add+remove / push
        self._last_fetch = 0.0

    def _g(self, *args: str, ok: tuple[int, ...] = (0,)):
        return git("--git-dir", str(self.git_dir), *args, ok=ok)

    async def init(self) -> None:
        async with self.lock:
            if not self.git_dir.exists():
                await git("init", "-q", "--bare", str(self.git_dir))
                await self._g("remote", "add", "origin", f"https://github.com/{self.name}.git")
                # Remote-tracking refs, so fetching never collides with branches checked out in worktrees.
                await self._g("config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
            sym = await self._g("ls-remote", "--symref", "origin", "HEAD")
            if m := re.search(r"ref: refs/heads/(\S+)\s+HEAD", sym):
                self.default_branch = m.group(1)
            await self._g("worktree", "prune")
        await self.fetch(force=True)

    async def fetch(self, force: bool = False) -> None:
        if not force and time.monotonic() - self._last_fetch < 60:
            return
        async with self.lock:
            await self._g("fetch", "-q", "--prune", "origin")
        self._last_fetch = time.monotonic()

    async def resolve(self, ref: str | None = None) -> str:
        """Resolve "main", a tag, a sha, or "pr/123" to a commit sha."""
        ref = ref or self.default_branch
        if ref.startswith("-"):
            raise GitError("bad ref")
        if m := re.fullmatch(r"(?:pr/|#)(\d+)", ref):
            async with self.lock:
                await self._g("fetch", "-q", "origin", f"+pull/{m[1]}/head:refs/remotes/pr/{m[1]}")
            ref = f"refs/remotes/pr/{m[1]}"
        for cand in (f"refs/remotes/origin/{ref}", ref):
            if sha := (await self._g("rev-parse", "-q", "--verify", f"{cand}^{{commit}}", ok=(0, 1))).strip():
                return sha
        raise GitError(f"unknown ref: {ref}")

    async def list_files(self, tree: str) -> list[str]:
        return [f for f in (await self._g("ls-tree", "-r", "--name-only", "-z", tree)).split("\0") if f]

    async def show(self, tree: str, path: str) -> str:
        return await self._g("show", f"{tree}:{path}")

    async def grep(self, tree: str, pattern: str, pathspec: str | None, ignore_case: bool) -> list[str]:
        args = ["grep", "-n", "-I", "--no-color", "-E", *(["-i"] if ignore_case else []), "-e", pattern, tree]
        if pathspec:
            args += ["--", pathspec]
        out = await self._g(*args, ok=(0, 1))
        return [line[len(tree) + 1:] for line in out.splitlines() if line]

    async def log(self, ref: str, n: int, path: str | None, pickaxe: str | None) -> str:
        args = ["log", "--no-color", f"-n{n}", "--format=%h %ad %an: %s", "--date=short"]
        if pickaxe:
            args.append(f"-S{pickaxe}")
        args.append(ref)
        if path:
            args += ["--", path]
        return await self._g(*args)

    async def add_worktree(self, base: str) -> Worktree:
        wid = secrets.token_hex(3)
        branch = f"bot/{date.today().isoformat()}-{wid}"
        d = self.data_dir / "wt" / self.slug / wid
        async with self.lock:
            await self._g("worktree", "add", "-q", "-b", branch, str(d), base)
        tree = (await git("rev-parse", "HEAD^{tree}", cwd=d)).strip()
        return Worktree(d, branch, tree)

    async def remove_worktree(self, wt: Worktree) -> None:
        async with self.lock:
            try:
                await self._g("worktree", "remove", "--force", str(wt.dir))
            except GitError:
                shutil.rmtree(wt.dir, ignore_errors=True)
                await self._g("worktree", "prune")
            await self._g("branch", "-D", wt.branch, ok=(0, 1))

    async def push(self, wt: Worktree) -> None:
        async with self.lock:
            await git("push", "-q", "origin", f"HEAD:refs/heads/{wt.branch}", cwd=wt.dir)


def safe_path(root: Path, p: str) -> Path:
    """Resolve a repo-relative path inside a worktree, refusing escapes and .git."""
    rel = posixpath.normpath(p.replace("\\", "/").lstrip("/").removeprefix("./"))
    if rel in ("", ".") or rel.startswith("..") or re.search(r"(^|/)\.git(/|$)", rel):
        raise GitError(f"invalid path: {p}")
    return root / rel


async def snapshot(wt: Worktree) -> None:
    await git("add", "-A", cwd=wt.dir)
    wt.tree = (await git("write-tree", cwd=wt.dir)).strip()
