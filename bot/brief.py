"""Per-repo brief placed at the start of the system prompt.

It gives the model a map of the repo up front so it doesn't spend steps rediscovering the layout.
The text stays the same between requests, so OpenAI's automatic prompt caching bills it at ~10%.
Source, first found: owner notes (data/briefs/<owner__repo>.md, set via /agent brief), then the
repo's AGENTS.md / CLAUDE.md, then the start of its README. Always followed by a depth-2 tree.
"""

from __future__ import annotations

from pathlib import Path

from .gitrepo import GitError, Repo
from .tools import compact_tree

DOC_CHARS = 2500
TREE_CHARS = 1500
_cache: dict[tuple[str, str], str] = {}


def notes_path(data_dir: Path, repo: str) -> Path:
    return data_dir / "briefs" / f"{repo.replace('/', '__')}.md"


def invalidate(repo: str) -> None:
    for k in [k for k in _cache if k[0] == repo]:
        del _cache[k]


async def get_brief(repo: Repo, sha: str, data_dir: Path) -> str:
    if (repo.name, sha) in _cache:
        return _cache[(repo.name, sha)]
    files = await repo.list_files(sha)
    doc, src = "", ""
    notes = notes_path(data_dir, repo.name)
    if notes.exists():
        doc, src = notes.read_text(), "owner notes"
    else:
        top = {f.lower(): f for f in files if "/" not in f}
        for cand in ("agents.md", "claude.md", "readme.md", "readme.rst", "readme"):
            if cand in top:
                try:
                    doc, src = await repo.show(sha, top[cand]), top[cand]
                    break
                except GitError:
                    pass
    if len(doc) > DOC_CHARS:
        doc = doc[:DOC_CHARS] + "\n…"
    tree = compact_tree(files, "", 2, limit=200)
    if len(tree) > TREE_CHARS:
        tree = compact_tree(files, "", 1, limit=200)[:TREE_CHARS]
    text = (f"## {repo.name} (default branch {repo.default_branch}, {len(files)} files)\n"
            + (f"### From {src}\n{doc.strip()}\n" if doc else "")
            + f"### Layout\n{tree}")
    _cache[(repo.name, sha)] = text
    return text
