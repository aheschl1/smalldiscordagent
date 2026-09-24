"""Preflight: verify every credential and permission the bot needs. python -m bot.check"""

from __future__ import annotations

import asyncio
import os

import httpx

from .config import Config
from .gitrepo import git

OK, BAD, WARN = "✅", "❌", "⚠️ "


async def main() -> None:
    cfg = Config.load()
    failed = False

    def report(ok: bool | None, label: str, detail: str = "") -> None:
        nonlocal failed
        failed |= ok is False
        print(f"{OK if ok else BAD if ok is False else WARN} {label}" + (f": {detail}" if detail else ""))

    async with httpx.AsyncClient(timeout=20) as h:
        # Discord
        tok = os.environ.get("DISCORD_TOKEN", "")
        if not tok:
            report(False, "DISCORD_TOKEN", "not set")
        else:
            r = await h.get("https://discord.com/api/v10/users/@me", headers={"Authorization": f"Bot {tok}"})
            report(r.status_code == 200, "Discord token", r.json().get("username", r.text[:100]))
            if r.status_code == 200:
                app = (await h.get("https://discord.com/api/v10/applications/@me",
                                   headers={"Authorization": f"Bot {tok}"})).json()
                flags = app.get("flags", 0)
                # GATEWAY_MESSAGE_CONTENT (1<<19) or _LIMITED (1<<18)
                report(bool(flags & (1 << 19 | 1 << 18)), "Message Content intent",
                       "enabled" if flags & (1 << 19 | 1 << 18) else "enable it in the Developer Portal → Bot")
                g = await h.get("https://discord.com/api/v10/users/@me/guilds", headers={"Authorization": f"Bot {tok}"})
                names = [x["name"] for x in g.json()] if g.status_code == 200 else []
                report(bool(names) or None, "Servers", ", ".join(names) or "bot isn't in any server yet")
        report(bool(cfg.owner_ids) or None, "OWNER_ID", ", ".join(map(str, cfg.owner_ids)) or "not set; nobody can run /agent")

        # OpenAI
        key = os.environ.get("OPENAI_API_KEY", "")
        r = await h.get(f"https://api.openai.com/v1/models/{cfg.model}", headers={"Authorization": f"Bearer {key}"})
        report(r.status_code == 200, f"OpenAI key + model {cfg.model}", "" if r.status_code == 200 else r.text[:150])

        # GitHub
        gh_tok = os.environ.get("GITHUB_TOKEN", "")
        report(bool(gh_tok) or None, "GITHUB_TOKEN", "set" if gh_tok else "not set (public repos only, no PRs)")
        hdr = {"Authorization": f"Bearer {gh_tok}"} if gh_tok else {}
        for repo in cfg.repos:
            base = f"https://api.github.com/repos/{repo}"
            probes = {"Contents": f"{base}/contents/", "Pull requests": f"{base}/pulls?per_page=1",
                      "Checks": f"{base}/commits/HEAD/check-runs?per_page=1",
                      "Actions": f"{base}/actions/runs?per_page=1"}
            for name, url in probes.items():
                r = await h.get(url, headers=hdr)
                report(r.status_code == 200 if name in ("Contents", "Pull requests") else (r.status_code == 200 or None),
                       f"{repo} · {name} (read)", "" if r.status_code == 200 else f"HTTP {r.status_code}")
            try:
                await git("ls-remote", "--heads", f"https://github.com/{repo}.git")
                report(True, f"{repo} · git clone access")
            except Exception as e:
                report(False, f"{repo} · git clone access", str(e)[:150])
            r = await h.get(base, headers=hdr)
            push = r.json().get("permissions", {}).get("push") if r.status_code == 200 else None
            report(bool(push) or None, f"{repo} · your account can push",
                   "" if push else "write users won't be able to open PRs")

        # Linear (optional)
        lk = os.environ.get("LINEAR_API_KEY", "")
        if not lk:
            report(None, "LINEAR_API_KEY", "not set; Linear tools disabled")
        else:
            r = await h.post("https://api.linear.app/graphql", headers={"Authorization": lk},
                             json={"query": "{ viewer { name } teams { nodes { key name } } }"})
            d = r.json()
            if r.status_code == 200 and not d.get("errors"):
                teams = ", ".join(t["key"] for t in d["data"]["teams"]["nodes"])
                report(True, "Linear", f"as {d['data']['viewer']['name']}; teams: {teams}")
                team = os.environ.get("LINEAR_TEAM", "")
                if team and team.upper() not in teams.upper().split(", "):
                    report(False, "LINEAR_TEAM", f"{team} is not one of {teams}")
            else:
                report(False, "Linear", str(d.get("errors") or r.text)[:150])

    print("\nAll good; start with: uv run python -m bot" if not failed else "\nFix the ❌ items above.")


if __name__ == "__main__":
    asyncio.run(main())
