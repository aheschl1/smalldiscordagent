FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY bot ./bot
# Mount config.toml and a persistent data/ volume; pass secrets via env.
VOLUME /app/data
CMD ["uv", "run", "--no-sync", "python", "-m", "bot"]
