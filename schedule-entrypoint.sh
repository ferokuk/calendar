#!/bin/sh
set -e

# Первый запуск сразу
python3 /app/ruz-to-ics.py

# Настраиваем cron (каждые 6 часов).
# У cron урезанный PATH (/usr/bin:/bin), в котором нет /usr/local/bin,
# поэтому подставляем абсолютный путь к python3 на этапе генерации crontab.
echo "0 */6 * * * cd /app && $(command -v python3) ruz-to-ics.py >> /proc/1/fd/1 2>&1" | crontab -

# Запускаем cron на переднем плане
cron -f
