"""Decide whether to jump in on a message nobody addressed to the bot, using Jev (TypeSafe's classifier).

One cheap call (~$0.00002) scores the latest message plus a little recent chat. Only confident
"answer" / "ticket" decisions reach the main model. Every decision is appended to data/triage.jsonl
so thresholds can be tuned from real traffic (shadow mode logs without acting).
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx

log = logging.getLogger("triage")

API = "https://api.typesafe.ai/v1/systemone"
KEY = os.environ.get("TYPESAFE_API_KEY", "")
MODEL = os.environ.get("TYPESAFE_MODEL", "jev-1.13.0")  # pinned: thresholds are tuned per model version

_client = httpx.AsyncClient(timeout=15, headers={"Authorization": f"Bearer {KEY}"})

QUESTIONS = {
    "action": {
        "type": "choice",
        "instructions": "What should the project's code assistant bot do about the LATEST message?",
        "criteria": {
            "answer": "It asks a technical question about the project's code, build, tooling, errors, tests or CI "
                      "that someone could answer by reading the repository or CI logs, and it isn't answered yet",
            "ticket": "It reports a bug, requests a feature, or describes work that should be tracked as a ticket, "
                      "and nobody has said it's already tracked or fixed",
            "ignore": "Anything else: chit-chat, jokes, logistics, scheduling, opinions, thanks, status updates, "
                      "questions about people or non-technical topics, or already answered",
        },
    },
    "to_person": {
        "type": "noul",
        "instructions": "The LATEST message is directed at a specific person (by name, @mention, or as a direct "
                        "reply to them) rather than asked to the group",
    },
}


def enabled() -> bool:
    return bool(KEY)


@dataclass
class Decision:
    decision: str                 # answer | ticket | ignore
    reason: str = ""
    probs: dict = field(default_factory=dict)
    to_person: float = 0.0
    model: str = ""


def build_state(recent: list[tuple[str, str]], author: str, content: str, repos: list[str]) -> str:
    lines = "\n".join(f"[{a}] {c[:400]}" for a, c in recent[-5:]) or "(none)"
    return (f"Discord server for a software project (repos: {', '.join(repos)}). A code assistant bot can read the "
            f"code, CI and issue tracker.\n\nRecent messages:\n{lines}\n\nLATEST message:\n[{author}] {content[:1500]}")


async def classify(state: str, answer_at: float, ticket_at: float, tickets_ok: bool) -> Decision:
    r = await _client.post(API, json={"state": state, "model": MODEL, "questions": QUESTIONS})
    r.raise_for_status()
    data = r.json()
    ans = data["answers"]
    probs = ans["action"]["probabilities"]
    to_person = float(ans["to_person"].get("noul", 0.0))
    d = Decision("ignore", probs=probs, to_person=to_person, model=data.get("model", ""))
    if to_person >= 0.6:
        d.reason = "directed at a person"
    elif probs.get("answer", 0) >= answer_at:
        d.decision = "answer"
    elif probs.get("ticket", 0) >= ticket_at:
        if tickets_ok:
            d.decision = "ticket"
        else:
            d.reason = "ticket-worthy, but Linear isn't configured"
    else:
        d.reason = "below threshold"
    return d


class TriageLog:
    def __init__(self, data_dir: Path):
        self.file = data_dir / "triage.jsonl"

    def write(self, msg_id: int, channel: str, author: str, content: str, d: Decision, mode: str, acted: str) -> None:
        row = {"ts": datetime.now(UTC).isoformat(timespec="seconds"), "msg": msg_id, "channel": channel,
               "author": author, "content": content[:300], "mode": mode, "acted": acted, **asdict(d)}
        with self.file.open("a") as f:
            f.write(json.dumps(row) + "\n")

    def recent(self, hours: float = 24) -> list[dict]:
        if not self.file.exists():
            return []
        cutoff = time.time() - hours * 3600
        out = []
        for line in self.file.read_text().splitlines()[-5000:]:
            try:
                row = json.loads(line)
                if datetime.fromisoformat(row["ts"]).timestamp() >= cutoff:
                    out.append(row)
            except (ValueError, KeyError):
                continue
        return out
