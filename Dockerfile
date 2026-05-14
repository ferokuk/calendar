FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends cron && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY ruz-to-ics.py homework-bot.py schedule-entrypoint.sh ./
RUN chmod +x schedule-entrypoint.sh
