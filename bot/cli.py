"""Run the agent without Discord: python -m bot.cli [--write] [--repo owner/name] "question" """

from __future__ import annotations

import argparse
import asyncio
import logging

from .agent import Agent
from .config import Config


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("question")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--repo")
    ap.add_argument("--session", default="cli")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")
    for noisy in ("httpx", "httpx2", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    cfg = Config.load()
    agent = Agent(cfg)
    await agent.start()

    async def progress(s: str) -> None:
        print(f"  > {s}")

    reply = await agent.run(key=args.session, question=args.question, user_id=0, user_name="cli",
                            level="write" if args.write else "read", repo=args.repo or cfg.repos[0],
                            on_progress=progress)
    print("\n" + reply.text + ("\n\n-# " + reply.footer if reply.footer else ""))


if __name__ == "__main__":
    asyncio.run(main())
