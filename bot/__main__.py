"""Entry point: python -m bot"""

import logging
import os

from .agent import Agent
from .config import Config
from .discord_bot import Bot


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s: %(message)s")
    for noisy in ("httpx", "httpx2", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    cfg = Config.load()
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise SystemExit("DISCORD_TOKEN is not set")
    Bot(cfg, Agent(cfg)).run(token, log_handler=None)


if __name__ == "__main__":
    main()
