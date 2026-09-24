"""Long-term memories the agent saves itself; rendered into the system prompt.

Global memories apply everywhere; repo memories only when that repo is the default for the chat.
The tools are only offered to write users, since memories shape every future answer for everyone.
Each scope is capped so the prompt can't grow without bound; when full, the agent must consolidate.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

SCOPE_CHARS = 2000  # per scope (global, each repo)
ITEM_CHARS = 300

_file: Path | None = None


def configure(data_dir: Path) -> None:
    global _file
    _file = data_dir / "memories.json"


def _load() -> dict:
    if _file and _file.exists():
        return json.loads(_file.read_text())
    return {"next": 1, "global": [], "repos": {}}


def _save(d: dict) -> None:
    assert _file
    tmp = _file.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=1))
    tmp.replace(_file)


def _scope(d: dict, repo: str | None) -> list:
    return d["global"] if repo is None else d["repos"].setdefault(repo, [])


def all_items(repo: str | None = None) -> list[tuple[str, dict]]:
    """[(scope label, item)] for global and one repo (or all repos if repo is None)."""
    d = _load()
    out = [("global", m) for m in d["global"]]
    for r, items in d["repos"].items():
        if repo is None or r == repo:
            out += [(r, m) for m in items]
    return out


def add(text: str, repo: str | None, by: str) -> str:
    text = " ".join(text.split())[:ITEM_CHARS]
    if not text:
        return "error: empty memory"
    d = _load()
    items = _scope(d, repo)
    if sum(len(m["text"]) for m in items) + len(text) > SCOPE_CHARS:
        return (f"error: {'repo' if repo else 'global'} memory is full. Consolidate: forget outdated or "
                "overlapping items (you can re-save a merged version), then try again.")
    mid = f"m{d['next']}"
    d["next"] += 1
    items.append({"id": mid, "text": text, "by": by, "at": datetime.now(UTC).strftime("%Y-%m-%d")})
    _save(d)
    return f"saved {mid}"


def forget(mid: str) -> bool:
    d = _load()
    for items in [d["global"], *d["repos"].values()]:
        for m in items:
            if m["id"] == mid:
                items.remove(m)
                _save(d)
                return True
    return False


def clear(repo: str | None = None, everything: bool = False) -> int:
    d = _load()
    n = 0
    if everything:
        n = len(d["global"]) + sum(len(v) for v in d["repos"].values())
        d["global"], d["repos"] = [], {}
    else:
        n = len(_scope(d, repo))
        _scope(d, repo).clear()
    _save(d)
    return n


def render(repo: str) -> str:
    """System-prompt section. Same text until a memory changes, so it stays prompt-cacheable."""
    d = _load()
    lines = [f"- [{m['id']}] {m['text']} ({m['by']}, {m['at'][5:]})" for m in d["global"]]
    rl = [f"- [{m['id']}] {m['text']} ({m['by']}, {m['at'][5:]})" for m in d["repos"].get(repo, [])]
    if not lines and not rl:
        return ""
    out = "## Memories (saved by you from past conversations; may be outdated)\n"
    if lines:
        out += "Global:\n" + "\n".join(lines) + "\n"
    if rl:
        out += f"{repo}:\n" + "\n".join(rl) + "\n"
    return out


# ---------------------------------------------------------------- tools

def _schema(name, desc, props, required=()):
    return {"type": "function", "name": name, "description": desc, "strict": False,
            "parameters": {"type": "object", "properties": props, "required": list(required)}}


REMEMBER = _schema(
    "remember",
    "Save one durable fact to long-term memory (shown in your prompt in future chats). For team conventions, "
    "preferences, decisions, ownership, recurring gotchas. Not for one-off details, secrets, or what's in the code.",
    {"text": {"type": "string", "description": "one short, self-contained fact"},
     "scope": {"type": "string", "enum": ["repo", "global"], "description": "repo (default) or global"}},
    ("text",))
FORGET = _schema("forget", "Delete a memory by id (e.g. m4) when it's wrong, outdated, or being merged.",
                 {"id": {"type": "string"}}, ("id",))


async def _remember(a: dict, c) -> str:
    repo = None if a.get("scope") == "global" else c.default_repo
    return add(a["text"], repo, c.requester)


async def _forget(a: dict, c) -> str:
    return "forgotten" if forget(a["id"].strip()) else f"no memory {a['id']}"


def tools() -> list:
    return [(REMEMBER, _remember), (FORGET, _forget)]
