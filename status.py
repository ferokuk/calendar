"""
Состояние процессов на диске: что и когда отработало, что упало.

Оба компонента хранят свои секции в data/status.json. На Linux обновления
защищены межпроцессной блокировкой, чтобы cron и бот не затирали друг друга.
"""

import json
import logging
import os
from contextlib import contextmanager
from datetime import datetime

import config

log = logging.getLogger("status")


@contextmanager
def _locked():
    """Сериализует запись между контейнерами, использующими общий data volume."""
    lock_path = config.STATUS_JSON.with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock_file:
        try:
            import fcntl
        except ImportError:  # pragma: no cover — локальный запуск на Windows
            fcntl = None
        if fcntl:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def now_iso() -> str:
    return datetime.now(tz=config.TZ).isoformat(timespec="seconds")


def _read_all() -> dict:
    path = config.STATUS_JSON
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("корневое значение должно быть объектом")
        return data
    except (OSError, json.JSONDecodeError, ValueError) as e:
        log.warning("Не удалось прочитать %s: %s", path.name, e)
        return {}


def read(component: str) -> dict:
    return _read_all().get(component, {})


def update(component: str, **fields) -> dict:
    """Дописывает поля в состояние компонента. Ошибки записи не фатальны."""
    try:
        with _locked():
            all_state = _read_all()
            state = all_state.get(component, {})
            if not isinstance(state, dict):
                state = {}
            state.update(fields)
            all_state[component] = state

            path = config.STATUS_JSON
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(all_state, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)
    except OSError as e:
        log.warning("Не удалось записать %s: %s", config.STATUS_JSON.name, e)
        return fields
    return state


def human_time(iso: str | None) -> str:
    """ISO-время → «02.09.2026 14:03 (3 ч назад)»."""
    if not iso:
        return "—"
    try:
        moment = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=config.TZ)
    stamp = moment.strftime("%d.%m.%Y %H:%M")
    delta = datetime.now(tz=config.TZ) - moment
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return stamp
    if seconds < 60:
        ago = "только что"
    elif seconds < 3600:
        ago = f"{seconds // 60} мин назад"
    elif seconds < 86400:
        ago = f"{seconds // 3600} ч назад"
    else:
        ago = f"{seconds // 86400} дн назад"
    return f"{stamp} ({ago})"
