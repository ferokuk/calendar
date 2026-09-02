#!/bin/sh
set -e

# Конфигурационные ошибки фатальны, сетевой сбой первого обновления — нет:
# алерт уже отправлен самим скриптом, а cron должен остаться жить и повторить позже.
if uv run --frozen --no-dev --no-sync python /app/ruz_schedule.py; then
    :
else
    FIRST_RUN_EXIT=$?
    if [ "$FIRST_RUN_EXIT" -eq 2 ]; then
        exit "$FIRST_RUN_EXIT"
    fi
    echo "Первичное обновление не удалось; cron продолжит работу"
fi

# Cron планирует задачи по системной зоне контейнера.
if [ ! -f "/usr/share/zoneinfo/${TZ:-Europe/Moscow}" ]; then
    echo "Не найдена таймзона: ${TZ:-Europe/Moscow}" >&2
    exit 2
fi
ln -snf "/usr/share/zoneinfo/${TZ:-Europe/Moscow}" /etc/localtime

# Cron не передаёт job-процессу окружение контейнера. Сохраняем его внутри
# контейнера, а маленький runner восстановит env до импорта config.py.
uv run --frozen --no-dev --no-sync python /app/cron-runner.py --snapshot

# Настраиваем cron. Расписание уже проверено config.py при первом запуске.
# У cron урезанный PATH (/usr/bin:/bin), в котором нет /usr/local/bin,
# поэтому подставляем абсолютный путь к python3 на этапе генерации crontab.
CRON_EXPRESSION="${SCHEDULE_CRON:-0 */6 * * *}"
UV_BIN="$(command -v uv)"
{
    echo "TZ=${TZ:-Europe/Moscow}"
    echo "$CRON_EXPRESSION cd /app && $UV_BIN run --frozen --no-dev --no-sync python /app/cron-runner.py >> /proc/1/fd/1 2>&1"
} | crontab -

# Запускаем cron на переднем плане
exec cron -f
