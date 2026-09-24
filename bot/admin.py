"""/agent slash commands. Everything except `whoami` is restricted to OWNER_ID. Replies are public."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Literal

import discord
from discord import app_commands

from . import memory, triage
from .brief import invalidate, notes_path

if TYPE_CHECKING:
    from .discord_bot import Bot

Level = Literal["none", "read", "write"]


def build_commands(bot: Bot) -> app_commands.Group:
    st = bot.state
    cfg = bot.cfg

    # Budget overrides set via /agent budget persist in state.json on top of config.toml.
    for k, v in st.s.get("budget", {}).items():
        setattr(cfg.budget, k, v)

    async def owner(inter: discord.Interaction) -> bool:
        if inter.user.id in cfg.owner_ids:
            return True
        await inter.response.send_message("Only the bot owner can do that.")
        return False

    async def done(inter: discord.Interaction, text: str) -> None:
        st.save()
        await inter.response.send_message(text)

    async def repo_autocomplete(_inter: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return [app_commands.Choice(name=r, value=r) for r in cfg.repos if current.lower() in r.lower()][:25]

    agent = app_commands.Group(name="agent", description="Repo agent settings")
    perm = app_commands.Group(name="perm", description="Who can use the bot", parent=agent)
    chan = app_commands.Group(name="channel", description="Where the bot responds", parent=agent)

    @agent.command(name="whoami", description="Show your access level")
    async def whoami(inter: discord.Interaction):
        roles = [r.id for r in getattr(inter.user, "roles", [])]
        spent = bot.agent.ledger.today()["users"].get(str(inter.user.id), 0.0)
        cap = cfg.budget.user_daily_usd
        pct = min(100, round(spent / cap * 100)) if cap > 0 else 0
        await inter.response.send_message(
            f"Your level: **{st.level_for(inter.user.id, roles)}** · {pct}% of today's budget used")

    @perm.command(name="user", description="Set a user's level (overrides their roles)")
    async def perm_user(inter: discord.Interaction, user: discord.User, level: Level):
        if await owner(inter):
            st.s["users"][str(user.id)] = level
            await done(inter, f"{user.mention} → **{level}**")

    @perm.command(name="role", description="Set a role's level")
    async def perm_role(inter: discord.Interaction, role: discord.Role, level: Level):
        if await owner(inter):
            st.s["roles"][str(role.id)] = level
            await done(inter, f"{role.mention} → **{level}**")

    @perm.command(name="default", description="Level for everyone without a user/role grant")
    async def perm_default(inter: discord.Interaction, level: Level):
        if await owner(inter):
            st.s["default_level"] = level
            await done(inter, f"Default level → **{level}**")

    @perm.command(name="clear", description="Remove a user or role grant")
    async def perm_clear(inter: discord.Interaction, user: discord.User | None = None,
                         role: discord.Role | None = None):
        if await owner(inter):
            if user:
                st.s["users"].pop(str(user.id), None)
            if role:
                st.s["roles"].pop(str(role.id), None)
            await done(inter, "Cleared." if user or role else "Nothing to clear; pass a user or role.")

    @chan.command(name="add", description="Allow the bot in a channel (switches from 'all channels' to a list)")
    async def chan_add(inter: discord.Interaction, channel: discord.TextChannel):
        if await owner(inter):
            if str(channel.id) not in st.s["channels"]:
                st.s["channels"].append(str(channel.id))
            await done(inter, f"Allowed in {channel.mention}. Channels: {_chans(st)}")

    @chan.command(name="remove", description="Stop responding in a channel")
    async def chan_remove(inter: discord.Interaction, channel: discord.TextChannel):
        if await owner(inter):
            if str(channel.id) in st.s["channels"]:
                st.s["channels"].remove(str(channel.id))
            note = "" if st.s["channels"] else " (list is empty, so the bot now responds in all channels)"
            await done(inter, f"Removed {channel.mention}. Channels: {_chans(st)}{note}")

    @chan.command(name="all", description="Respond in all channels")
    async def chan_all(inter: discord.Interaction):
        if await owner(inter):
            st.s["channels"] = []
            await done(inter, "Responding in all channels.")

    @agent.command(name="mention", description="Require an @mention to respond (threads the bot started never do)")
    async def mention(inter: discord.Interaction, required: bool):
        if await owner(inter):
            st.s["require_mention"] = required
            await done(inter, f"Mention required: **{required}**")

    @agent.command(name="repo", description="Set the default repo for a channel")
    @app_commands.autocomplete(repo=repo_autocomplete)
    async def repo_cmd(inter: discord.Interaction, channel: discord.TextChannel, repo: str):
        if await owner(inter):
            if repo not in cfg.repos:
                return await inter.response.send_message(f"Unknown repo. Configured: {', '.join(cfg.repos)}")
            st.s["channel_repos"][str(channel.id)] = repo
            await done(inter, f"{channel.mention} → `{repo}`")

    @agent.command(name="brief", description="Set notes the agent always sees for a repo (empty text clears)")
    @app_commands.autocomplete(repo=repo_autocomplete)
    async def brief(inter: discord.Interaction, repo: str, text: str | None = None):
        if await owner(inter):
            if repo not in cfg.repos:
                return await inter.response.send_message("Unknown repo.")
            p = notes_path(cfg.data_dir, repo)
            if text:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(text.replace("\\n", "\n"))
            else:
                p.unlink(missing_ok=True)
            invalidate(repo)
            await done(inter, f"Brief for `{repo}` {'set' if text else 'cleared (using AGENTS.md/README)'}.")

    @agent.command(name="budget", description="Show spend, or set limits in USD")
    async def budget(inter: discord.Interaction, daily: float | None = None, monthly: float | None = None,
                     per_user_daily: float | None = None, per_request: float | None = None):
        if not await owner(inter):
            return
        b = cfg.budget
        changes = {"daily_usd": daily, "monthly_usd": monthly, "user_daily_usd": per_user_daily,
                   "request_usd": per_request}
        for k, v in changes.items():
            if v is not None:
                setattr(b, k, v)
                st.s.setdefault("budget", {})[k] = v
        led = bot.agent.ledger
        top = sorted(led.today()["users"].items(), key=lambda kv: -kv[1])[:5]
        await done(inter,
                   f"Today ${led.today()['total']:.2f} / ${b.daily_usd:.2f} (fallback to `{cfg.fallback_model}` at "
                   f"{b.fallback_at:.0%}) · month ${led.month():.2f} / ${b.monthly_usd:.2f}\n"
                   f"Per user/day ${b.user_daily_usd:.2f} · per request ${b.request_usd:.2f}\n"
                   + ("Top today: " + ", ".join(f"<@{u}> ${v:.2f}" for u, v in top) if top else ""))

    mem = app_commands.Group(name="memory", description="The agent's saved memories", parent=agent)

    @mem.command(name="list", description="Show saved memories")
    async def mem_list(inter: discord.Interaction):
        if await owner(inter):
            items = memory.all_items()
            text = "\n".join(f"`{m['id']}` [{scope}] {m['text']} ({m['by']}, {m['at']})" for scope, m in items)
            await inter.response.send_message((text or "No memories yet.")[:1990])

    @mem.command(name="forget", description="Delete a memory by id (e.g. m4)")
    async def mem_forget(inter: discord.Interaction, memory_id: str):
        if await owner(inter):
            ok = memory.forget(memory_id.strip())
            await inter.response.send_message(f"Forgot `{memory_id}`." if ok else f"No memory `{memory_id}`.")

    @mem.command(name="add", description="Save a memory yourself")
    @app_commands.autocomplete(repo=repo_autocomplete)
    async def mem_add(inter: discord.Interaction, text: str, repo: str | None = None):
        if await owner(inter):
            if repo and repo not in cfg.repos:
                return await inter.response.send_message("Unknown repo.")
            await inter.response.send_message(memory.add(text, repo, inter.user.display_name))

    @mem.command(name="clear", description="Delete all memories")
    async def mem_clear(inter: discord.Interaction):
        if await owner(inter):
            await inter.response.send_message(f"Cleared {memory.clear(everything=True)} memories.")

    listen = app_commands.Group(name="listen", description="Unprompted replies to messages that don't mention the bot",
                                parent=agent)

    @listen.command(name="add", description="Watch a channel for questions/tickets without a mention")
    async def listen_add(inter: discord.Interaction, channel: discord.TextChannel):
        if await owner(inter):
            if str(channel.id) not in st.s["listen_channels"]:
                st.s["listen_channels"].append(str(channel.id))
            key = "" if triage.enabled() else " (TYPESAFE_API_KEY isn't set, so nothing will happen yet)"
            await done(inter, f"Watching {channel.mention} in **{st.s['listen_mode']}** mode.{key}")

    @listen.command(name="remove", description="Stop watching a channel")
    async def listen_remove(inter: discord.Interaction, channel: discord.TextChannel):
        if await owner(inter):
            if str(channel.id) in st.s["listen_channels"]:
                st.s["listen_channels"].remove(str(channel.id))
            await done(inter, f"Stopped watching {channel.mention}.")

    @listen.command(name="mode", description="shadow: classify and log only · live: actually reply / suggest tickets")
    async def listen_mode(inter: discord.Interaction, mode: Literal["shadow", "live"]):
        if await owner(inter):
            st.s["listen_mode"] = mode
            await done(inter, f"Listen mode → **{mode}**")

    @listen.command(name="tune", description="Set thresholds (0-1), cooldown (seconds) and daily cap")
    async def listen_tune(inter: discord.Interaction, answer_at: float | None = None, ticket_at: float | None = None,
                          cooldown_s: int | None = None, daily_max: int | None = None):
        if await owner(inter):
            for k, v in {"listen_answer_at": answer_at, "listen_ticket_at": ticket_at,
                         "listen_cooldown_s": cooldown_s, "listen_daily_max": daily_max}.items():
                if v is not None:
                    st.s[k] = v
            await done(inter, f"answer ≥ {st.s['listen_answer_at']} · ticket ≥ {st.s['listen_ticket_at']} · "
                              f"cooldown {st.s['listen_cooldown_s']}s/channel · max {st.s['listen_daily_max']}/day")

    @listen.command(name="stats", description="What triage decided recently")
    async def listen_stats(inter: discord.Interaction, hours: int = 24):
        if not await owner(inter):
            return
        rows = bot.triage_log.recent(hours)
        if not rows:
            return await inter.response.send_message(f"No triaged messages in the last {hours}h.")
        counts: dict[str, int] = {}
        for r in rows:
            counts[r["decision"]] = counts.get(r["decision"], 0) + 1
        chans = ", ".join(f"<#{c}>" for c in st.s["listen_channels"]) or "none"
        lines = [f"**{len(rows)}** messages in {hours}h · mode **{st.s['listen_mode']}** · watching {chans}",
                 " · ".join(f"{k}: {v}" for k, v in sorted(counts.items()))]
        picks = [r for r in rows if r["decision"] != "ignore"][-6:]
        near = sorted((r for r in rows if r["decision"] == "ignore" and r.get("reason") == "below threshold"),
                      key=lambda r: -max(r["probs"].get("answer", 0), r["probs"].get("ticket", 0)))[:3]
        if picks:
            lines.append("\n**Would act / acted on:**")
            lines += [_row(r) for r in picks]
        if near:
            lines.append("\n**Closest misses:**")
            lines += [_row(r) for r in near]
        await inter.response.send_message("\n".join(lines)[:1990])

    @agent.command(name="show", description="Show current settings")
    async def show(inter: discord.Interaction):
        if await owner(inter):
            s = dict(st.s)
            await inter.response.send_message(f"```json\n{json.dumps(s, indent=1)[:1800]}\n```\n"
                                              f"Repos: {', '.join(cfg.repos)} · model `{cfg.model}`")

    return agent


def _row(r: dict) -> str:
    p = r["probs"]
    return (f"-# answer {p.get('answer', 0):.2f} · ticket {p.get('ticket', 0):.2f} · {r['acted']}\n"
            f"> {r['author']}: {r['content'][:150]}")


def _chans(st) -> str:
    return ", ".join(f"<#{c}>" for c in st.s["channels"]) or "all"
