# repo-agent-bot

A Discord bot that answers questions about your GitHub repos, debugs from code and CI logs, reviews PRs, and opens
small PRs. The agent harness is written from scratch: one tool loop on the OpenAI Responses API, ~10 tools, and no shell.

## What it does

| Ask | What happens |
| --- | --- |
| `@bot where do we validate webhook signatures?` | greps/reads the repo, answers with `path:line` citations |
| `@bot why is this failing?` (as a reply to a pasted stack trace) | traces the error through the code, checks `log search=` for recent changes and `ci` for failing checks |
| `@bot review #123` | reads the PR metadata and patches plus surrounding code, checks CI, gives a verdict in Discord |
| `@bot post that review` | posts it to GitHub as a COMMENT review (write users only; never approves) |
| `@bot bump the timeout in config.py to 30s and open a PR` | edits in an isolated worktree, opens a PR (write users only) |

Each mention starts a Discord thread. That thread is the conversation: follow-ups there need no mention, keep context,
and further change requests push to the same PR.

## Setup

1. **Discord app**: create one at <https://discord.com/developers/applications>. Under **Bot**, enable the
   **Message Content Intent**. Invite it with the scopes `bot` and `applications.commands` and the permissions
   *View Channels, Send Messages, Create Public Threads, Send Messages in Threads, Read Message History*.
2. **GitHub token**: a fine-grained PAT limited to your repos, with Contents: read/write, Pull requests: read/write,
   Actions: read, Checks: read. For a read-only bot, grant read only.
3. **Configure**:
   ```bash
   cp .env.example .env              # DISCORD_TOKEN, OPENAI_API_KEY, GITHUB_TOKEN, OWNER_ID
   cp config.example.toml config.toml   # set repos = ["owner/name", ...]
   ```
4. **Run**:
   ```bash
   uv run python -m bot
   # or: docker build -t repo-agent . && docker run --env-file .env -v $PWD/config.toml:/app/config.toml -v $PWD/data:/app/data repo-agent
   ```
   Test without Discord: `uv run python -m bot.cli [--write] [--repo owner/name] "question"`.

## Linear (optional)

Set `LINEAR_API_KEY` (Linear → Settings → Account → Security & access → Personal API keys) and optionally
`LINEAR_TEAM` (default team key). Then:

- `@bot is there a ticket for the replay crash?` searches Linear (`linear_search`; an ID like `ENG-12` returns the
  full issue with comments).
- `@bot file a bug for this` (as a reply to an error) searches for duplicates, then creates the issue with a link
  back to the thread.
- `@bot comment on ENG-12 that the fix is in #45`.
- `@bot move ENG-12 to In Progress and assign it to Sam` requires **write**.

Anyone with `read` can search, create and comment; changing status, assignee, priority or title needs `write`.
Tickets are created as the API key's owner, so consider a dedicated Linear bot account.

## Managing it from Discord

Only the user(s) in `OWNER_ID` can run these. Replies are visible to everyone in the channel, and changes persist in
`data/state.json`.

| Command | |
| --- | --- |
| `/agent perm user <user> <none\|read\|write>` | per-user level; overrides that user's roles |
| | (owners in `OWNER_ID` always have `write`) |
| `/agent perm role <role> <level>` | per-role level; a user gets their highest role level |
| `/agent perm default <level>` | everyone else (default `read`) |
| `/agent perm clear [user] [role]` | remove a grant |
| `/agent channel add\|remove <channel>` / `/agent channel all` | channel allowlist (empty = all channels) |
| `/agent mention <true\|false>` | require an @mention (default true; threads the bot started never need one) |
| `/agent repo <channel> <repo>` | default repo for a channel |
| `/agent brief <repo> [text]` | notes the agent always sees for that repo (architecture, conventions, where things live). Empty clears. |
| `/agent budget [daily] [monthly] [per_user_daily] [per_request]` | show spend or change limits |
| `/agent show` | dump settings |
| `/agent whoami` | anyone: your level and today's usage |

## Permissions model

- `none`: ignored (gets a one-line refusal on mention).
- `read`: read tools only (`ls read grep log pr ci`). The model is never shown the write tools, so a read user
  can't talk it into making changes.
- `write`: adds `edit write open_pr review`. Edits happen in a throwaway git worktree on a `bot/…` branch; PRs are
  never merged by the bot, and reviews are posted as COMMENT only.

There's no shell tool, and nothing from the repo is ever executed. The GitHub token reaches git through environment
config, never `.git/config`, and the model never sees it. Repo, PR and CI text is treated as data (the prompt says so),
but still review bot PRs like any other contribution.

## Cost

The defaults assume roughly $2,500 of credit spread over about 2.5 months:

- Model `gpt-6-sol` ($2 / $0.20 cached / $10 per 1M tokens). Typical questions cost $0.01–0.05; deep debugging or
  reviews of large PRs cost $0.10–0.30.
- Budget: $30/day, $900/month, $5 per user per day, $1.50 per request. Past 80% of the daily cap the bot switches to
  `gpt-6-luna` rather than stopping. Spend is tracked in `data/usage.json`.

How it keeps tokens down:
- **Repo brief**: owner notes → `AGENTS.md`/`CLAUDE.md` → README, plus a depth-2 tree. It sits at the front of the
  prompt, so the model doesn't spend steps rediscovering the layout, and it's billed at the cached rate after the
  first call. Writing a good `/agent brief` is the best way to cut cost.
- **Small, stable prefix**: a short system prompt and terse tool schemas, with `prompt_cache_key` per repo and
  permission level.
- **Capped tool output**: 8k chars per call; grep returns at most 60 hits; reads are line-ranged; PR patches are
  paged per file; CI logs are reduced to error lines plus the tail.
- **Lean history**: threads keep only the last 6 question/answer pairs, with no tool traces.

## Layout

```
bot/config.py      config.toml + runtime state (perms, channels)
bot/gitrepo.py     bare clones; reads at a pinned commit; per-thread worktrees for edits
bot/github.py      PRs, reviews, check runs, job logs
bot/tools.py       tool schemas + implementations
bot/brief.py       per-repo brief
bot/agent.py       turn loop, sessions, budget ledger
bot/discord_bot.py routing, threads, progress, chunking
bot/admin.py       /agent commands
bot/cli.py         local testing
```
