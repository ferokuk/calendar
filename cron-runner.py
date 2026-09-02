"""Передаёт cron-задаче исходное окружение Docker-контейнера."""

import json
import os
import sys
from pathlib import Path


SNAPSHOT = Path("/run/ruz-calendar-env.json")


def snapshot() -> None:
    SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(SNAPSHOT, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        json.dump(dict(os.environ), output, ensure_ascii=False)


def run() -> None:
    with SNAPSHOT.open(encoding="utf-8") as source:
        os.environ.update(json.load(source))

    # Важно импортировать только после восстановления окружения: config.py
    # читает и валидирует env при импорте.
    import ruz_schedule

    ruz_schedule.main()


if __name__ == "__main__":
    if sys.argv[1:] == ["--snapshot"]:
        snapshot()
    else:
        run()
