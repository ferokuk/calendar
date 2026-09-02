FROM ghcr.io/astral-sh/uv:0.11.27 AS uv

FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends cron tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=uv /uv /uvx /bin/
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY config.py logs.py notify.py status.py ruz_schedule.py homework-bot.py cron-runner.py schedule-entrypoint.sh ./
RUN chmod +x schedule-entrypoint.sh
