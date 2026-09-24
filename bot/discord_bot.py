"""Discord wiring: message routing, permission checks, threads, progress updates, chunked replies."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import deque

import discord
from discord import app_commands

from . import linear, triage
from .admin import build_commands
from .agent import Agent
from .config import Config, StateStore

log = logging.getLogger("discord_bot")

MAX_MSG = 1990
MAX_QUOTE = 1500
NO_REPLY = "NO_REPLY"
PASSIVE_BILLING_ID = 0  # unprompted runs are billed to this ledger bucket (capped like any user), not the author


def chunk(text: str, limit: int = MAX_MSG) -> list[str]:
    """Split on line boundaries under Discord's limit, keeping ``` fences balanced across chunks."""
    out: list[str] = []
    cur = ""
    fence = ""  # the open fence line (e.g. "```py") if we're inside a code block
    for line in text.split("\n"):
        while len(line) > limit - 20:  # hard-wrap pathological lines
            head, line = line[: limit - 20], line[limit - 20:]
            cur, fence = _append(out, cur, head, fence, limit)
        cur, fence = _append(out, cur, line, fence, limit)
    if cur.strip():
        out.append(cur)
    return out or ["(empty)"]


def _append(out: list[str], cur: str, line: str, fence: str, limit: int) -> tuple[str, str]:
    if len(cur) + len(line) + 1 + (4 if fence else 0) > limit:
        out.append(cur + ("\n```" if fence else ""))
        cur = fence + "\n" if fence else ""
    cur += line + "\n"
    if line.strip().startswith("```"):
        fence = "" if fence else line.strip()
    return cur, fence


def unprompted_question(ctx: str, text: str) -> str:
    return (f"(Unprompted: nobody mentioned you. This channel message looked like a technical question you can help "
            f"with. Only answer if you can add real value. Reply exactly {NO_REPLY} if someone already answered it, "
            f"it's social or not really a question, or it's aimed at a specific person.)\n"
            f"Recent channel messages:\n{ctx}\n\nMessage:\n{text}")


class Bot(discord.Client):
    def __init__(self, cfg: Config, agent: Agent):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = False
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.cfg = cfg
        self.agent = agent
        self.state = StateStore(cfg.data_dir, cfg.owner_ids)
        self.triage_log = triage.TriageLog(cfg.data_dir)
        self._recent: dict[int, deque] = {}          # channel -> recent (author, text) for triage context
        self._last_passive: dict[int, float] = {}   # channel -> last unprompted reply time
        self._passive_runs: deque[float] = deque()  # timestamps of unprompted agent runs (daily cap)
        self._passive_msgs: set[int] = set()        # unprompted bot messages (❌ deletes them)
        self.tree = app_commands.CommandTree(self)
        self.tree.add_command(build_commands(self))

    async def setup_hook(self) -> None:
        await self.agent.start()

    async def on_ready(self) -> None:
        log.info("logged in as %s (%s) in %d guilds", self.user, self.user.id, len(self.guilds))
        if not self.cfg.owner_ids:
            log.warning("OWNER_ID is not set: nobody can run /agent admin commands")
        for g in self.guilds:
            await self._sync(g)

    async def on_guild_join(self, guild: discord.Guild) -> None:
        await self._sync(guild)

    async def _sync(self, guild: discord.Guild) -> None:
        # Per-guild sync makes commands available immediately (global sync can take up to an hour).
        self.tree.copy_global_to(guild=guild)
        try:
            await self.tree.sync(guild=guild)
        except discord.HTTPException as e:
            log.warning("command sync failed for %s: %s", guild.name, e)

    # ------------------------------------------------------------ routing

    async def on_message(self, msg: discord.Message) -> None:
        if not msg.guild or not self.user:
            return
        ch = msg.channel
        in_thread = isinstance(ch, discord.Thread)
        parent_id = ch.parent_id if in_thread else None
        listening = self.state.listening(ch.id, parent_id)
        recent: list[tuple[str, str]] = []
        if listening:  # rolling context for triage, including the bot's own messages
            buf = self._recent.setdefault(ch.id, deque(maxlen=8))
            recent = list(buf)
            buf.append((msg.author.display_name, msg.clean_content[:400]))
        if msg.author.bot:
            return
        if not (self.state.channel_allowed(ch.id, parent_id) or listening):
            return

        mentioned = self.user in msg.mentions
        ref = msg.reference.resolved if msg.reference else None  # gateway usually includes the replied-to message
        replied_to_bot = isinstance(ref, discord.Message) and ref.author.id == self.user.id
        addressed = mentioned or replied_to_bot or not self.state.s["require_mention"]
        if not addressed and in_thread and ch.owner_id == self.user.id:
            # In a thread the bot started, only the person who started it can talk without a mention,
            # so several people chatting in the thread don't each trigger a reply.
            sess = self.agent.session(str(ch.id))
            addressed = sess.meta.setdefault("starter", msg.author.id) == msg.author.id  # old threads: first speaker
        if not addressed:
            if listening and not (in_thread and ch.owner_id == self.user.id):
                asyncio.create_task(self._passive(msg, recent))
            return

        roles = [r.id for r in getattr(msg.author, "roles", [])]
        level = self.state.level_for(msg.author.id, roles)
        if level == "none":
            if mentioned:
                await msg.reply("You don't have access to this bot.", mention_author=False)
            return

        question = re.sub(rf"<@!?{self.user.id}>", "", msg.content).strip()
        if not question:
            if mentioned:
                await msg.reply("Ask me something about the code.", mention_author=False)
            return
        if msg.attachments:
            question += "\n(attachments: " + ", ".join(a.filename for a in msg.attachments) + " — not readable)"
        # Replying to someone's message (e.g. a pasted stack trace) brings it in as context.
        if msg.reference and msg.reference.message_id:
            try:
                ref = ref or await ch.fetch_message(msg.reference.message_id)
                if isinstance(ref, discord.Message) and ref.content:
                    question = f"(replying to {ref.author.display_name}: \"\"\"{ref.content[:MAX_QUOTE]}\"\"\")\n{question}"
            except discord.HTTPException:
                pass

        # A mention in a normal channel starts a thread; the thread is the conversation/session.
        # Replying to one of the bot's in-channel messages continues inline instead.
        target: discord.abc.Messageable = ch
        if not in_thread and isinstance(ch, discord.TextChannel) and not replied_to_bot:
            try:
                target = await msg.create_thread(name=_thread_name(question), auto_archive_duration=1440)
                sess = self.agent.session(str(target.id))
                sess.meta["starter"] = msg.author.id
                self.agent.save_session(sess)
            except discord.HTTPException:
                target = ch  # no thread permission: answer inline
        key = str(target.id) if target is not ch or in_thread else f"chan:{ch.id}"
        await self._answer(msg, target, key, question, level, self._repo(ch.id, parent_id))

    def _repo(self, channel_id: int, parent_id: int | None) -> str:
        repo = self.state.repo_for(channel_id, parent_id, self.cfg.repos[0])
        return repo if repo in self.cfg.repos else self.cfg.repos[0]

    # ------------------------------------------------------------ unprompted replies (Jev triage)

    async def _passive(self, msg: discord.Message, recent: list[tuple[str, str]]) -> None:
        st = self.state.s
        text = msg.clean_content.strip()
        if not triage.enabled() or len(text) < 12 or not re.search(r"[A-Za-z]{3}", text):
            return
        roles = [r.id for r in getattr(msg.author, "roles", [])]
        if self.state.level_for(msg.author.id, roles) == "none":
            return
        ch = msg.channel
        try:
            d = await triage.classify(triage.build_state(recent, msg.author.display_name, text, self.cfg.repos),
                                      st["listen_answer_at"], st["listen_ticket_at"], linear.enabled())
        except Exception as e:
            log.warning("triage failed: %s", e)
            return

        acted = "none"
        if d.decision != "ignore":
            if st["listen_mode"] != "live":
                acted = "shadow"
            elif time.time() - self._last_passive.get(ch.id, 0) < st["listen_cooldown_s"]:
                acted = "skipped: channel cooldown"
            elif self._passive_count() >= st["listen_daily_max"]:
                acted = "skipped: daily cap"
            elif not self.agent.pick_model(PASSIVE_BILLING_ID)[0]:
                acted = "skipped: budget"
            else:
                self._last_passive[ch.id] = time.time()
                self._passive_runs.append(time.time())
                try:
                    acted = await (self._jump_in(msg, recent) if d.decision == "answer"
                                   else self._suggest_ticket(msg, recent))
                except Exception as e:
                    log.exception("unprompted %s failed", d.decision)
                    acted = f"error: {type(e).__name__}"
        name = getattr(ch, "name", str(ch.id))
        self.triage_log.write(msg.id, name, msg.author.display_name, text, d, st["listen_mode"], acted)
        log.info("triage #%s %s -> %s (%s) acted=%s", name, msg.id, d.decision,
                 ", ".join(f"{k}={v:.2f}" for k, v in d.probs.items()), acted)

    def _passive_count(self) -> int:
        cutoff = time.time() - 86400
        while self._passive_runs and self._passive_runs[0] < cutoff:
            self._passive_runs.popleft()
        return len(self._passive_runs)

    async def _jump_in(self, msg: discord.Message, recent: list[tuple[str, str]]) -> str:
        """Answer inline (no thread) as a reply to the message; the agent may decline with NO_REPLY."""
        ch = msg.channel
        ctx = "\n".join(f"[{a}] {c}" for a, c in recent[-5:]) or "(none)"
        question = unprompted_question(ctx, msg.clean_content[:MAX_QUOTE])
        key = f"chan:{ch.id}"
        # Read-only regardless of who wrote the message: nobody asked for changes.
        reply = await self.agent.run(key=key, question=question, user_id=PASSIVE_BILLING_ID,
                                     user_name=msg.author.display_name, level="read", repo=self._repo(ch.id,
                                     getattr(ch, "parent_id", None)),
                                     extra_tools=[history_tool(msg, ch, skip={msg.id})], origin=msg.jump_url)
        if not reply.text or reply.text.strip().strip(".").upper() == NO_REPLY:
            sess = self.agent.session(key)
            if sess.history and sess.history[-1][1].strip().strip(".").upper() == NO_REPLY:
                sess.history.pop()
                self.agent.save_session(sess)
            return "declined"
        parts = chunk(reply.text)
        sent = [await msg.reply(parts[0], mention_author=False)]
        for p in parts[1:]:
            sent.append(await ch.send(p))
        self._passive_msgs.update(m.id for m in sent)
        return "replied"

    async def _suggest_ticket(self, msg: discord.Message, recent: list[tuple[str, str]]) -> str:
        view = TicketView(self, msg, recent)
        m = await msg.reply("This sounds worth tracking. Want me to file a Linear ticket for it?", view=view,
                            mention_author=False)
        self._passive_msgs.add(m.id)
        return "suggested ticket"

    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        # Anyone can remove an unprompted reply with ❌.
        if str(payload.emoji) not in ("❌", "✖️") or payload.message_id not in self._passive_msgs:
            return
        ch = self.get_channel(payload.channel_id)
        if ch is None:
            return
        try:
            m = await ch.fetch_message(payload.message_id)
            await m.delete()
            self._passive_msgs.discard(payload.message_id)
        except discord.HTTPException:
            pass

    async def _answer(self, msg: discord.Message, target, key: str, question: str, level: str, repo: str) -> None:
        status = await (target.send("Working…") if target is not msg.channel
                        else msg.reply("Working…", mention_author=False))
        steps: list[str] = []
        last_edit = 0.0

        async def progress(s: str) -> None:
            nonlocal last_edit
            steps.append(s)
            if time.monotonic() - last_edit < 1.5:  # stay well under edit rate limits
                return
            last_edit = time.monotonic()
            shown = "\n".join(f"-# {x[:120]}" for x in steps[-6:])
            try:
                await status.edit(content=f"Working…\n{shown}")
            except discord.HTTPException:
                pass

        try:
            async with target.typing():
                reply = await self.agent.run(key=key, question=question, user_id=msg.author.id,
                                             user_name=msg.author.display_name, level=level, repo=repo,
                                             on_progress=progress,
                                             extra_tools=[history_tool(msg, target, skip={msg.id, status.id})],
                                             origin=getattr(target, "jump_url", msg.jump_url),
                                             confirm=lambda summary: ask_confirm(target, msg.author, summary))
            text = reply.text + (f"\n-# {reply.footer}" if reply.footer else "")
        except Exception as e:  # surface failures in-channel rather than going silent
            log.exception("agent failed")
            text = f"⚠️ Something went wrong: `{type(e).__name__}: {str(e)[:300]}`"

        parts = chunk(text)
        await status.edit(content=parts[0])
        for p in parts[1:]:
            await target.send(p)


CONFIRM_TIMEOUT_S = 300
TICKET_VIEW_TIMEOUT_S = 6 * 3600


class TicketView(discord.ui.View):
    """[Create] files a Linear ticket for an unprompted suggestion; [Dismiss] deletes the suggestion."""

    def __init__(self, bot: Bot, msg: discord.Message, recent: list[tuple[str, str]]):
        super().__init__(timeout=TICKET_VIEW_TIMEOUT_S)
        self.bot, self.msg, self.recent = bot, msg, recent

    @discord.ui.button(label="Create ticket", style=discord.ButtonStyle.primary)
    async def create(self, inter: discord.Interaction, _button: discord.ui.Button) -> None:
        roles = [r.id for r in getattr(inter.user, "roles", [])]
        level = self.bot.state.level_for(inter.user.id, roles)
        if level == "none":
            await inter.response.send_message("You don't have access to this bot.", ephemeral=True)
            return
        await inter.response.edit_message(content=f"Filing a ticket (requested by {inter.user.display_name})…",
                                          view=None)
        self.stop()
        ch = self.msg.channel
        ctx = "\n".join(f"[{a}] {c}" for a, c in self.recent[-5:]) or "(none)"
        question = (f"File a Linear ticket for the message below from {self.msg.author.display_name}. Search for "
                    f"duplicates first; if one exists, comment on it instead and link it. Short title, factual "
                    f"description quoting the message, plus a link to it: {self.msg.jump_url}\n"
                    f"Recent channel messages:\n{ctx}\n\nMessage:\n{self.msg.clean_content[:MAX_QUOTE]}")
        try:
            reply = await self.bot.agent.run(
                key=f"chan:{ch.id}", question=question, user_id=inter.user.id, user_name=inter.user.display_name,
                level=level, repo=self.bot._repo(ch.id, getattr(ch, "parent_id", None)),
                extra_tools=[history_tool(self.msg, ch, skip=set())], origin=self.msg.jump_url)
            text = reply.text
        except Exception as e:
            log.exception("ticket creation failed")
            text = f"⚠️ Couldn't file the ticket: `{type(e).__name__}: {str(e)[:300]}`"
        await inter.edit_original_response(content=chunk(text)[0])

    @discord.ui.button(label="Dismiss", style=discord.ButtonStyle.secondary)
    async def dismiss(self, inter: discord.Interaction, _button: discord.ui.Button) -> None:
        await inter.response.defer()
        self.stop()
        try:
            await inter.message.delete()
        except discord.HTTPException:
            pass



class ConfirmView(discord.ui.View):
    """Confirm/Cancel buttons that only the requesting user can press."""

    def __init__(self, user: discord.abc.User):
        super().__init__(timeout=CONFIRM_TIMEOUT_S)
        self.user = user
        self.approved = False

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if inter.user.id != self.user.id:
            await inter.response.send_message(f"Only {self.user.display_name} can confirm this.", ephemeral=True)
            return False
        return True

    async def _finish(self, inter: discord.Interaction, approved: bool) -> None:
        self.approved = approved
        verdict = f"**Confirmed** by {inter.user.display_name}" if approved else f"**Cancelled** by {inter.user.display_name}"
        await inter.response.edit_message(content=f"{inter.message.content}\n{verdict}", view=None)
        self.stop()

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, inter: discord.Interaction, _button: discord.ui.Button) -> None:
        await self._finish(inter, True)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, inter: discord.Interaction, _button: discord.ui.Button) -> None:
        await self._finish(inter, False)


async def ask_confirm(target: discord.abc.Messageable, user: discord.abc.User, summary: str) -> bool:
    view = ConfirmView(user)
    text = f"{user.mention}, the agent wants to run this. Confirm?\n{summary}"
    prompt = await target.send(text[:1990], view=view,
                               allowed_mentions=discord.AllowedMentions(users=[user]))
    timed_out = await view.wait()
    if timed_out:
        try:
            await prompt.edit(content=f"{prompt.content}\n**Timed out**; not run.", view=None)
        except discord.HTTPException:
            pass
    return view.approved


HISTORY_SCHEMA = {
    "type": "function", "name": "discord_history", "strict": False,
    "description": "Read Discord messages, newest last. Default: this thread/channel. `channel`: 'list' (all channels "
                   "and active threads), 'parent' (this thread's channel), 'all' (search every channel; needs "
                   "`search`), or a channel name/id. `before`: message id to page back. `search`: filter by text/author.",
    "parameters": {"type": "object", "properties": {
        "channel": {"type": "string"}, "limit": {"type": "integer", "description": "default 30, max 100"},
        "before": {"type": "string"}, "search": {"type": "string"},
    }, "required": []},
}

SCAN_PER_CHANNEL = 300  # messages scanned per channel for channel='all' searches


def _fmt(m: discord.Message, where: str = "") -> str:
    text = m.clean_content  # mentions rendered as names
    if m.attachments:
        text += " [attachments: " + ", ".join(x.filename for x in m.attachments) + "]"
    if not text and m.embeds:
        text = "[embed] " + (m.embeds[0].title or m.embeds[0].description or "")[:200]
    text = text.replace("\n", " ⏎ ")
    who = m.author.display_name + (" (bot)" if m.author.bot else "")
    return f"[{m.created_at:%m-%d %H:%M}]{where} {who}: {text if len(text) <= 500 else text[:500] + '…'}"


def _matches(m: discord.Message, q: str) -> bool:
    return q in m.clean_content.lower() or q in m.author.display_name.lower()


def _readable(guild: discord.Guild) -> list:
    """Every text channel and active thread the bot itself can read."""
    me = guild.me
    chans = [*guild.text_channels, *guild.threads]
    return [c for c in chans if c.permissions_for(me).read_message_history and c.permissions_for(me).view_channel]


def history_tool(msg: discord.Message, here: discord.abc.Messageable, skip: set[int]):
    """discord_history: read and search any channel the bot can see in this server."""

    async def impl(a: dict, _ctx) -> str:
        guild = msg.guild
        want = (a.get("channel") or "").strip().lstrip("#")
        search = (a.get("search") or "").lower()
        limit = max(1, min(int(a.get("limit") or 30), 100))

        if want == "list":
            rows = []
            for c in _readable(guild):
                if isinstance(c, discord.Thread):
                    rows.append(f"  thread #{c.name} (in #{c.parent.name if c.parent else '?'}) id={c.id}")
                else:
                    cat = f"[{c.category.name}] " if c.category else ""
                    topic = f" — {c.topic[:100]}" if c.topic else ""
                    rows.append(f"{cat}#{c.name} id={c.id}{topic}")
            return "\n".join(rows) or "no readable channels"

        if want == "all":
            if not search:
                return "error: channel='all' needs `search`"

            async def scan(c) -> list[tuple[discord.Message, str]]:
                hits = []
                try:
                    async for m in c.history(limit=SCAN_PER_CHANNEL):
                        if m.id not in skip and _matches(m, search):
                            hits.append((m, c.name))
                except discord.HTTPException:
                    pass
                return hits

            found = [h for hs in await asyncio.gather(*(scan(c) for c in _readable(guild))) for h in hs]
            found.sort(key=lambda h: h[0].created_at)
            rows = [_fmt(m, f" #{name}") for m, name in found[-limit:]]
            return (f"{len(found)} matches across channels (last {SCAN_PER_CHANNEL} msgs each), showing "
                    f"{len(rows)}:\n" + ("\n".join(rows) or "(none)"))

        ch = here
        if want == "parent":
            ch = getattr(here, "parent", None) or msg.channel
            if isinstance(ch, discord.Thread):
                ch = ch.parent
        elif want:
            if want.strip("<>").isdigit():
                ch = guild.get_channel_or_thread(int(want.strip("<>")))
            else:
                ch = next((c for c in _readable(guild) if c.name.lower() == want.lower()), None)
            if ch is None:
                return f"error: no channel {want!r}; use channel='list'"
        if not hasattr(ch, "history"):
            return "error: that channel has no message history"

        before = discord.Object(int(a["before"])) if str(a.get("before") or "").isdigit() else None
        rows: list[str] = []
        oldest = None
        async for m in ch.history(limit=500 if search else limit + len(skip), before=before):
            oldest = m
            if m.id in skip or (search and not _matches(m, search)):
                continue
            rows.append(_fmt(m))
            if len(rows) >= limit:
                break
        rows.reverse()
        out = f"#{getattr(ch, 'name', 'channel')}: {len(rows)} messages\n" + ("\n".join(rows) or "(none)")
        if oldest is not None:
            out += f"\n[older: before={oldest.id}]"
        return out

    return HISTORY_SCHEMA, impl


def _thread_name(q: str) -> str:
    q = re.sub(r"\s+", " ", re.sub(r"<[@#][!&]?\d+>", "", q)).strip()
    return (q[:80] + "…") if len(q) > 80 else (q or "question")
