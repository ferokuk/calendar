#!/bin/sh
set -e

# Первый запуск сразу
python3 /app/ruz-to-ics.py

# Настраиваем cron (каждые 6 часов)
echo "0 */6 * * * cd /app && python ruz-to-ics.py >> /proc/1/fd/1 2>&1" | crontab -

# Запускаем cron на переднем плане
cron -f
