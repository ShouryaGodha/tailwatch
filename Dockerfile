# tailwatch: the scheduler and the Slack bot in one small image.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY prompts ./prompts
COPY skills ./skills
COPY presets ./presets
COPY docs ./docs
RUN pip install ".[slack,postgres,mcp]"

# Your tailwatch.yaml, .env, watchlist and database live in /data (a volume).
WORKDIR /data
ENTRYPOINT ["tailwatch"]
CMD ["up"]
