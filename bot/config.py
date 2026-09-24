"""Static config (config.toml + env) and runtime state (data/state.json, changed via /agent commands)."""

from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

LEVELS = ("none", "read", "write")


def _rank(level: str) -> int:
    return LEVELS.index(level)


@dataclass
class Budget:
    daily_usd: float = 30.0
    monthly_usd: float = 900.0
    user_daily_usd: float = 5.0
    request_usd: float = 1.5      # stop tool use and force an answer past this
    fallback_at: float = 0.8      # switch to fallback_model after this fraction of the daily budget


@dataclass
class Config:
    repos: list[str]
    model: str = "gpt-6-sol"
    fallback_model: str = "gpt-6-luna"
    reasoning_effort: str | None = "medium"
    max_steps: int = 30
    max_tool_output: int = 8000   # chars per tool result
    history_turns: int = 6        # prior Q/A pairs kept per thread (answers only, no tool traces)
    draft_prs: bool = False
    show_usage: bool = False
    data_dir: Path = Path("data")
    budget: Budget = field(default_factory=Budget)
    prices: dict[str, list[float]] = field(default_factory=dict)  # model -> [input, cached, output] per 1M

    owner_ids: set[int] = field(default_factory=set)

    @classmethod
    def load(cls, path: str | None = None) -> Config:
        path = path or os.environ.get("CONFIG", "config.toml")
        raw = tomllib.loads(Path(path).read_text()) if Path(path).exists() else {}
        budget = Budget(**raw.pop("budget", {}))
        cfg = cls(**raw, budget=budget)
        if not cfg.repos:
            raise SystemExit(f"No repos configured in {path}")
        for r in cfg.repos:
            if not re.fullmatch(r"[\w.-]+/[\w.-]+", r):
                raise SystemExit(f"Bad repo name: {r}")
        cfg.data_dir = Path(cfg.data_dir).resolve()
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        cfg.owner_ids = {int(x) for x in os.environ.get("OWNER_ID", "").replace(" ", "").split(",") if x}
        return cfg

    def price(self, model: str) -> list[float]:
        # Dated snapshots ("gpt-6-sol-2026-...") fall back to their base model's price.
        for name, p in self.prices.items():
            if model == name or model.startswith(name + "-"):
                return p
        return [0.0, 0.0, 0.0]


STATE_DEFAULTS = {
    "channels": [],          # channel IDs; empty = all channels
    "require_mention": True,
    "default_level": "read",  # for users with no user/role grant
    "users": {},             # user ID -> level
    "roles": {},             # role ID -> level
    "channel_repos": {},     # channel ID -> default repo
    # Unprompted replies (Jev triage) for messages that don't mention the bot
    "listen_channels": [],   # channel IDs to watch (when listen_all is off)
    "listen_all": False,     # watch every channel the bot can see, except listen_exclude
    "listen_exclude": [],
    "listen_mode": "shadow",  # shadow = classify + log only; live = reply / suggest tickets
    "listen_answer_at": 0.8,
    "listen_ticket_at": 0.85,
    "listen_cooldown_s": 600,  # min seconds between unprompted replies per channel
    "listen_daily_max": 40,   # unprompted agent runs per UTC day
}


class StateStore:
    def __init__(self, data_dir: Path, owner_ids: set[int] | None = None):
        self.owner_ids = owner_ids or set()
        self.file = data_dir / "state.json"
        saved = json.loads(self.file.read_text()) if self.file.exists() else {}
        self.s: dict = {**json.loads(json.dumps(STATE_DEFAULTS)), **saved}

    def save(self) -> None:
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.s, indent=2))
        tmp.replace(self.file)

    def level_for(self, user_id: int, role_ids: list[int]) -> str:
        """Owners always write. Else an explicit user grant wins (so a user can be downgraded below a role);
        else best role; else default."""
        if user_id in self.owner_ids:
            return "write"
        if (u := self.s["users"].get(str(user_id))) is not None:
            return u
        roles = [self.s["roles"][str(r)] for r in role_ids if str(r) in self.s["roles"]]
        return max(roles, key=_rank) if roles else self.s["default_level"]

    def channel_allowed(self, channel_id: int, parent_id: int | None = None) -> bool:
        chans = self.s["channels"]
        return not chans or str(channel_id) in chans or (parent_id is not None and str(parent_id) in chans)

    def listening(self, channel_id: int, parent_id: int | None = None) -> bool:
        ids = {str(channel_id)} | ({str(parent_id)} if parent_id is not None else set())
        if self.s["listen_all"]:
            return not ids & set(self.s["listen_exclude"])
        return bool(ids & set(self.s["listen_channels"]))

    def repo_for(self, channel_id: int, parent_id: int | None, default: str) -> str:
        cr = self.s["channel_repos"]
        return cr.get(str(channel_id)) or (cr.get(str(parent_id)) if parent_id else None) or default
