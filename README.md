# Small Discord Agent

A Discord bot for your GitHub repos. It can answer questions about the code, debug errors, review PRs, open small
PRs, and search Discord and Linear for context.

## Setup

1. **Discord:** create an app at <https://discord.com/developers/applications>. Under **Bot**, copy the token and
   enable **Message Content Intent**. Invite it with the `bot` and `applications.commands` scopes.
2. **GitHub:** create a fine-grained token for your repos. For full use it needs Contents, Pull requests, Issues,
   Actions and Administration (read/write), plus Checks (read). Grant less and the bot can do less.
3. **Configure:**
   ```bash
   cp .env.example .env                 # tokens + OWNER_ID (your Discord user ID)
   cp config.example.toml config.toml   # repos = ["owner/name"]
   ```
4. **Check and run:**
   ```bash
   uv run python -m bot.check   # verifies every token and permission
   uv run python -m bot         # start the bot
   uv run python -m bot.cli "how does X work?"   # try the agent without Discord
   ```

## Using it

Mention the bot to start a thread: `@Computa why does the replay viewer crash?`

- The person who started the thread can follow up without mentioning the bot. Anyone else mentions it or replies
  to one of its messages.
- Reply to a message (such as a pasted error) while mentioning the bot to include that message as context.
- Conversations are saved to disk and survive restarts.

## Permissions

| Level | Can |
| --- | --- |
| `none` | nothing |
| `read` (default) | ask questions, debug, review in chat, read Discord/GitHub/Linear, create and comment on Linear tickets |
| `write` | also edit code, open PRs, make any GitHub or Linear change, and have the agent save memories |

Owners (`OWNER_ID`) always have `write`. A read user is never given the write tools, and the bot has no shell.
**Destructive actions** (merge, close, delete, settings, releases, workflow runs, Linear deletes and archives) show
the exact call with **Confirm / Cancel** buttons. Only the person who asked can click them, and nothing runs until
they do.

## Memories

While talking with a write user, the agent saves lasting facts (team conventions, decisions, who owns what,
recurring gotchas) on its own. They're added to every later prompt, either for all repos or just for one. They're
capped at about 2,000 characters per scope, so the agent merges or forgets old ones as it goes. Manage them with
`/agent memory list|add|forget|clear`.

## Jumping in without a mention (optional)

With `TYPESAFE_API_KEY` set, the bot can watch channels you pick. Each message that doesn't mention it is scored by
[Jev](https://docs.typesafe.ai/), a fast classifier that costs about $0.00002 per message:

- **Technical question:** Computa replies in the channel, with no thread. It uses read-only tools and stays quiet if
  it has nothing useful to add.
- **Bug or task worth tracking:** it offers to file a Linear ticket with **Create / Dismiss** buttons, and never
  files one on its own.
- Anyone can react ❌ to delete an unprompted reply. There's a per-channel cooldown and a daily cap.

It starts in **shadow** mode, which scores and logs but posts nothing. Check `/agent listen stats` for a few days,
adjust thresholds with `/agent listen tune`, then switch to `/agent listen mode live`.

## Owner commands

```
/agent perm user|role <who> <none|read|write>   /agent perm default <level>   /agent perm clear
/agent channel add|remove <channel>             /agent channel all
/agent mention <true|false>                     /agent repo <channel> <repo>
/agent brief <repo> [notes]                     /agent budget [limits]
/agent memory list|add|forget|clear             /agent show
/agent listen add|remove <channel>              /agent listen all|off
/agent listen mode <shadow|live>
/agent listen tune [thresholds, cooldown, cap]  /agent listen stats [hours]
/agent whoami   (anyone)
```

`/agent brief` notes are added to the start of every prompt for that repo. Good notes (architecture, where things
live) are the best way to make answers faster and cheaper.

## Linear (optional)

Add `LINEAR_API_KEY` and, optionally, `LINEAR_TEAM` to `.env`. Tickets are created as the key's owner.

## Cost

It uses `gpt-6-sol` by default. Most answers cost $0.01–0.05. The default limits are $30/day, $900/month, $5 per
user per day and $1.50 per request, and the bot switches to `gpt-6-luna` after 80% of the daily limit. You can
change these in `config.toml` or with `/agent budget`. Each answer's cost is logged.
