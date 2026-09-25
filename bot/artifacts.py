"""Text files attached to Discord messages, stored on disk as artifacts the agent reads with tools.

Only the id and a short summary go into the question, so big logs don't bloat the prompt or session history.
Ids are a hash of the content: the same file attached twice is one artifact.
Stored under data/artifacts/<id>.txt with <id>.json metadata; pruned after ARTIFACT_KEEP_S unused.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

ARTIFACT_KEEP_S = 30 * 86400

_dir: Path | None = None


def configure(data_dir: Path) -> None:
    global _dir
    _dir = data_dir / "artifacts"
    _dir.mkdir(parents=True, exist_ok=True)


def _paths(aid: str) -> tuple[Path, Path]:
    assert _dir
    if not re.fullmatch(r"a[0-9a-f]{10}", aid):
        raise ValueError(f"bad artifact id {aid!r}")
    return _dir / f"{aid}.txt", _dir / f"{aid}.json"


def save(data: bytes, filename: str, author: str, url: str) -> tuple[str, dict]:
    """Store a text attachment; returns (id, metadata)."""
    text = data.decode("utf-8", errors="replace")
    aid = "a" + hashlib.sha256(data).hexdigest()[:10]
    body, meta_file = _paths(aid)
    if meta_file.exists():
        return aid, json.loads(meta_file.read_text())
    meta = {"filename": filename, "author": author, "url": url, "created": time.time(),
            "bytes": len(data), "lines": text.count("\n") + 1}
    body.write_text(text)
    meta_file.write_text(json.dumps(meta))
    return aid, meta


def load(aid: str) -> tuple[str, dict]:
    body, meta_file = _paths(aid.strip())
    if not meta_file.exists():
        raise ValueError(f"no artifact {aid}")
    meta_file.touch()  # last used, for pruning
    return body.read_text(), json.loads(meta_file.read_text())


def prune() -> int:
    if not _dir:
        return 0
    n = 0
    cutoff = time.time() - ARTIFACT_KEEP_S
    for f in _dir.glob("*.json"):
        if f.stat().st_mtime < cutoff:
            f.with_suffix(".txt").unlink(missing_ok=True)
            f.unlink(missing_ok=True)
            n += 1
    return n


# ---------------------------------------------------------------- tools

def _schema(name, desc, props, required=()):
    return {"type": "function", "name": name, "description": desc, "strict": False,
            "parameters": {"type": "object", "properties": props, "required": list(required)}}


ID = {"id": {"type": "string", "description": "artifact id, e.g. a1b2c3d4e5f"}}
READ = _schema("artifact_read", "Read an attached file (artifact) with line numbers. Default: 250 lines from start.",
               {**ID, "start": {"type": "integer"}, "end": {"type": "integer"}}, ("id",))
GREP = _schema("artifact_grep", "Regex search an attached file (artifact). Returns line:text.",
               {**ID, "pattern": {"type": "string"}, "ignore_case": {"type": "boolean"}}, ("id", "pattern"))


async def _read(a: dict, c) -> str:
    from .tools import clip
    lines = load(a["id"])[0].split("\n")
    s = max(1, int(a.get("start") or 1))
    e = min(len(lines), int(a.get("end") or s + 249))
    body = "\n".join(f"{s + i}\t{clip(l, 400)}" for i, l in enumerate(lines[s - 1:e]))
    return body + (f"\n[lines {s}-{e} of {len(lines)}]" if e < len(lines) or s > 1 else "")


async def _grep(a: dict, c) -> str:
    from .tools import clip
    try:
        rx = re.compile(a["pattern"], re.IGNORECASE if a.get("ignore_case") else 0)
    except re.error as e:
        return f"error: bad pattern: {e}"
    hits = [f"{i}:{clip(l, 240)}" for i, l in enumerate(load(a["id"])[0].split("\n"), 1) if rx.search(l)]
    if not hits:
        return "no matches"
    limit = 60
    return "\n".join(hits[:limit]) + (f"\n…{len(hits) - limit} more matches; narrow pattern" if len(hits) > limit else "")


def tools() -> list:
    return [(READ, _read), (GREP, _grep)]
