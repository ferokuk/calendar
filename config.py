"""
Конфигурация проекта: все значения читаются из переменных окружения.

Модуль общий для генератора расписания и телеграм-бота. Ошибки не бросаются
сразу при разборе — они копятся в списке, чтобы check() показал сразу все
проблемы .env, а не по одной за перезапуск контейнера.
"""

import logging
import os
from datetime import timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class ConfigError(RuntimeError):
    """Отсутствующий или некорректный параметр окружения."""


_errors: list[str] = []


def _raw(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _missing(name: str, hint: str) -> None:
    _errors.append(f"{name} — обязательная переменная ({hint})")


def _str(name: str, default: str | None = None, *, required: bool = False,
         hint: str = "") -> str | None:
    value = _raw(name)
    if value is None:
        if required:
            _missing(name, hint)
        return default
    return value


def _int(name: str, default: int | None = None, *, required: bool = False,
         minimum: int | None = None, maximum: int | None = None,
         hint: str = "") -> int | None:
    value = _raw(name)
    if value is None:
        if required:
            _missing(name, hint)
        return default
    try:
        number = int(value)
    except ValueError:
        _errors.append(f"{name}={value!r} — ожидается целое число")
        return default
    if minimum is not None and number < minimum:
        _errors.append(f"{name}={number} — значение меньше допустимого ({minimum})")
        return default
    if maximum is not None and number > maximum:
        _errors.append(f"{name}={number} — значение больше допустимого ({maximum})")
        return default
    return number


def _float(name: str, default: float, *, minimum: float | None = None) -> float:
    value = _raw(name)
    if value is None:
        return default
    try:
        number = float(value.replace(",", "."))
    except ValueError:
        _errors.append(f"{name}={value!r} — ожидается число")
        return default
    if minimum is not None and number < minimum:
        _errors.append(f"{name}={number} — значение меньше допустимого ({minimum})")
        return default
    return number


def _bool(name: str, default: bool) -> bool:
    value = _raw(name)
    if value is None:
        return default
    low = value.lower()
    if low in ("1", "true", "yes", "y", "on", "да"):
        return True
    if low in ("0", "false", "no", "n", "off", "нет"):
        return False
    _errors.append(f"{name}={value!r} — ожидается true/false")
    return default


def _path(name: str, default: str) -> Path:
    return Path(_str(name, default) or default)


def _timezone(name: str):
    value = _str(name, "Europe/Moscow") or "Europe/Moscow"
    try:
        return value, ZoneInfo(value)
    except ZoneInfoNotFoundError:
        if value == "Europe/Moscow":
            return value, timezone(timedelta(hours=3), name="MSK")
        if value in ("UTC", "Etc/UTC"):
            return value, timezone.utc
        _errors.append(f"{name}={value!r} — неизвестная таймзона IANA")
        # Оставляем модуль импортируемым, чтобы check() показал понятную
        # агрегированную ошибку даже когда в системе отсутствует база зон.
        return value, timezone(timedelta(hours=3), name="MSK")


def _cron(name: str, default: str) -> str:
    value = _str(name, default) or default
    unsafe = ("\n", "\r", "%", ";", "&", "|", "`", "$", ">", "<")
    if any(token in value for token in unsafe) or len(value.split()) != 5:
        _errors.append(f"{name}={value!r} — ожидается cron-выражение из пяти полей")
        return default
    return value


# ── Общее ──────────────────────────────────────────────────
LOG_LEVEL = (_str("LOG_LEVEL", "INFO") or "INFO").upper()
if LOG_LEVEL not in logging.getLevelNamesMapping():
    _errors.append(f"LOG_LEVEL={LOG_LEVEL!r} — неизвестный уровень логирования")

TZ_NAME, TZ = _timezone("TZ")
TZ_LABEL = _str("TZ_LABEL", "MSK")

DATA_DIR = _path("DATA_DIR", "/app/data")
CALENDARS_DIR = _path("CALENDARS_DIR", "/app/calendars")

# ── Telegram ───────────────────────────────────────────────
TG_BOT_TOKEN = _str("TG_BOT_TOKEN", required=True, hint="токен бота от @BotFather")
ADMIN_CHAT_ID = _int("ADMIN_CHAT_ID", required=True,
                     hint="твой Telegram user id, узнать можно у @userinfobot")
ALERTS_ENABLED = _bool("ALERTS_ENABLED", True)
ALERT_COOLDOWN_MINUTES = _int("ALERT_COOLDOWN_MINUTES", 60, minimum=0)

# ── Расписание РУЗ ─────────────────────────────────────────
RUZ_GROUP_ID = _int("RUZ_GROUP_ID", required=True,
                    hint="id группы в ruz.fa.ru, виден в адресе страницы расписания")
RUZ_API_URL = _str("RUZ_API_URL", "https://ruz.fa.ru/api/schedule/group")
RUZ_DAYS_AHEAD = _int("RUZ_DAYS_AHEAD", 60, minimum=1, maximum=365)
RUZ_CHUNK_DAYS = _int("RUZ_CHUNK_DAYS", 7, minimum=1, maximum=60)

# Подгруппы: MARKER помечает пары, которые вообще делятся на подгруппы,
# FILTER — какая из них твоя. Пустой FILTER отключает фильтрацию целиком.
SUBGROUP_MARKER = _str("SUBGROUP_MARKER", "")
SUBGROUP_FILTER = _str("SUBGROUP_FILTER", "")

HTTP_TIMEOUT = _float("HTTP_TIMEOUT", 15.0, minimum=1.0)
HTTP_RETRIES = _int("HTTP_RETRIES", 4, minimum=1, maximum=20)
HTTP_BACKOFF = _float("HTTP_BACKOFF", 3.0, minimum=0.0)
HTTP_BACKOFF_MAX = _float("HTTP_BACKOFF_MAX", 60.0, minimum=1.0)
FETCH_DELAY = _float("FETCH_DELAY", 1.0, minimum=0.0)
SCHEDULE_CRON = _cron("SCHEDULE_CRON", "0 */6 * * *")

# ── Домашка ────────────────────────────────────────────────
HOMEWORK_RETENTION_DAYS = _int("HOMEWORK_RETENTION_DAYS", 14, minimum=0)
HOMEWORK_CLEANUP_HOUR = _int("HOMEWORK_CLEANUP_HOUR", 4, minimum=0, maximum=23)

# ── Производные пути ───────────────────────────────────────
HOMEWORK_JSON = DATA_DIR / "homework.json"
TAGS_JSON = DATA_DIR / "tags.json"
ALERTS_JSON = DATA_DIR / "alerts.json"
STATUS_JSON = DATA_DIR / "status.json"
SCHEDULE_JSON = CALENDARS_DIR / "schedule.json"
HOMEWORK_ICS = CALENDARS_DIR / "homework.ics"


def check() -> None:
    """Бросает ConfigError со списком всех проблем окружения."""
    if _errors:
        raise ConfigError(
            "Некорректная конфигурация (проверь .env):\n"
            + "\n".join(f"  • {e}" for e in _errors)
        )
