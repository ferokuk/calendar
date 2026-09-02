"""Единая настройка логирования для всех процессов проекта."""

import logging
import sys
from datetime import datetime

import config


class _LocalTimeFormatter(logging.Formatter):
    """Пишет время в настроенной таймзоне и подписывает её (например MSK)."""

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        moment = datetime.fromtimestamp(record.created, tz=config.TZ)
        stamp = moment.strftime(datefmt or "%Y-%m-%d %H:%M:%S")
        return f"{stamp} {config.TZ_LABEL}" if config.TZ_LABEL else stamp


def setup(component: str) -> logging.Logger:
    """Настраивает корневой логгер и возвращает логгер компонента."""
    logging.addLevelName(logging.WARNING, "WARN")
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_LocalTimeFormatter(
        fmt="%(asctime)s %(levelname)s [%(name)s] %(message)s"
    ))

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, config.LOG_LEVEL, logging.INFO))

    # Библиотеки телеграма шумят на INFO про каждый HTTP-запрос
    for noisy in ("httpx", "telegram.ext.Application", "apscheduler"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    return logging.getLogger(component)


def now_str() -> str:
    """Текущее время в человекочитаемом виде — для сообщений в Telegram."""
    return datetime.now(tz=config.TZ).strftime("%d.%m.%Y %H:%M") + (
        f" {config.TZ_LABEL}" if config.TZ_LABEL else ""
    )
