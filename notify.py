"""
Уведомления админу в Telegram.

Работает через голый Bot API поверх urllib, а не через python-telegram-bot:
генератор расписания живёт в отдельном контейнере, у него нет polling-цикла,
и тянуть ради одного сообщения асинхронную библиотеку незачем.

Одинаковые ошибки не спамят: пока действует кулдаун, повторы копятся в
счётчике, а когда шаг снова отрабатывает — приходит «восстановилось».
"""

import json
import logging
import os
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta

import config
import logs

log = logging.getLogger("notify")

_API = "https://api.telegram.org/bot{token}/sendMessage"
_SEND_ATTEMPTS = 3
_SEND_BACKOFF = 2.0


@contextmanager
def _state_lock():
    """Защищает alerts.json от одновременной записи cron и ботом."""
    lock_path = config.ALERTS_JSON.with_suffix(".lock")
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = lock_path.open("a+b")
    except OSError as e:
        log.warning("Не удалось заблокировать состояние уведомлений: %s", e)
        yield
        return

    with lock_file:
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


def describe_error(error: BaseException | str) -> str:
    """Исключение → «TimeoutError: The read operation timed out»."""
    if isinstance(error, str):
        return error
    text = str(error).strip()
    return f"{type(error).__name__}: {text}" if text else type(error).__name__


# ── Состояние дедупликации ─────────────────────────────────
def _load_state() -> dict:
    if not config.ALERTS_JSON.exists():
        return {}
    try:
        state = json.loads(config.ALERTS_JSON.read_text(encoding="utf-8"))
        return state if isinstance(state, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(state: dict) -> None:
    tmp = config.ALERTS_JSON.with_suffix(".json.tmp")
    try:
        config.ALERTS_JSON.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, config.ALERTS_JSON)
    except OSError as e:
        log.warning("Не удалось сохранить состояние уведомлений: %s", e)


# ── Отправка ───────────────────────────────────────────────
def send(text: str) -> bool:
    """Шлёт сообщение админу. Возвращает True при успехе, ошибки не бросает."""
    if not config.ALERTS_ENABLED:
        log.debug("Уведомления отключены (ALERTS_ENABLED=false)")
        return False
    if not config.TG_BOT_TOKEN or not config.ADMIN_CHAT_ID:
        log.warning("Уведомление не отправлено: не задан TG_BOT_TOKEN или ADMIN_CHAT_ID")
        return False

    payload = json.dumps({
        "chat_id": config.ADMIN_CHAT_ID,
        "text": text,
        "disable_web_page_preview": True,
    }).encode("utf-8")
    request = urllib.request.Request(
        _API.format(token=config.TG_BOT_TOKEN),
        data=payload,
        headers={"Content-Type": "application/json"},
    )

    for attempt in range(1, _SEND_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(request, timeout=config.HTTP_TIMEOUT) as resp:
                resp.read()
            return True
        except Exception as e:  # noqa: BLE001 — уведомление не должно ронять процесс
            reason = describe_error(e)
            if attempt == _SEND_ATTEMPTS:
                log.error("Не удалось отправить уведомление в Telegram: %s", reason)
                return False
            pause = _SEND_BACKOFF * attempt
            log.warning(
                "Уведомление не ушло (попытка %d/%d): %s. Повтор через %.0f с",
                attempt, _SEND_ATTEMPTS, reason, pause,
            )
            time.sleep(pause)
    return False


# ── Алерты ─────────────────────────────────────────────────
def alert(title: str, error: BaseException | str, *, step: str = "",
          attempts: int | None = None, extra: dict | None = None,
          key: str | None = None) -> bool:
    """
    Сообщает об ошибке. `key` определяет, что считать «той же самой» ошибкой
    для кулдауна и для последующего recovered().
    """
    if not config.ALERTS_ENABLED:
        return False

    with _state_lock():
        error_text = describe_error(error)
        key = key or title
        state = _load_state()
        entry = state.get(key, {})

        if entry.get("active") and entry.get("error") == error_text:
            last_sent = entry.get("last_sent")
            if last_sent and _within_cooldown(last_sent):
                entry["suppressed"] = entry.get("suppressed", 0) + 1
                entry["last_seen"] = status_now()
                state[key] = entry
                _save_state(state)
                log.info(
                    "Та же ошибка «%s» уже отправлена, повтор подавлен (%d за кулдаун %d мин)",
                    key, entry["suppressed"], config.ALERT_COOLDOWN_MINUTES,
                )
                return False

        lines = [f"🔴 {title}", ""]
        if step:
            lines.append(f"Шаг: {step}")
        lines.append(f"Ошибка: {error_text}")
        if attempts is not None:
            lines.append(f"Попыток: {attempts}")
        for name, value in (extra or {}).items():
            lines.append(f"{name}: {value}")
        suppressed = entry.get("suppressed", 0)
        if suppressed:
            lines.append(f"Повторов с прошлого уведомления: {suppressed}")
        lines.append(f"Время: {logs.now_str()}")

        sent = send("\n".join(lines))
        state[key] = {
            "active": True,
            "error": error_text,
            "last_sent": status_now() if sent else entry.get("last_sent"),
            "last_seen": status_now(),
            "suppressed": 0 if sent else suppressed,
        }
        _save_state(state)
        return sent


def recovered(key: str, message: str) -> bool:
    """Сообщает, что упавший ранее шаг снова работает. Молчит, если падений не было."""
    if not config.ALERTS_ENABLED:
        return False

    with _state_lock():
        state = _load_state()
        entry = state.get(key)
        if not entry or not entry.get("active"):
            return False

        was = entry.get("error", "неизвестная ошибка")
        sent = send(f"✅ {message}\n\nБыло: {was}\nВремя: {logs.now_str()}")
        if sent:
            entry["active"] = False
            entry["recovered_at"] = status_now()
            entry["suppressed"] = 0
        state[key] = entry
        _save_state(state)
        return sent


def is_failing(key: str) -> bool:
    if not config.ALERTS_ENABLED:
        return False
    entry = _load_state().get(key)
    return bool(entry and entry.get("active"))


def status_now() -> str:
    return datetime.now(tz=config.TZ).isoformat(timespec="seconds")


def _within_cooldown(last_sent: str) -> bool:
    try:
        moment = datetime.fromisoformat(last_sent)
    except ValueError:
        return False
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=config.TZ)
    return datetime.now(tz=config.TZ) - moment < timedelta(minutes=config.ALERT_COOLDOWN_MINUTES)
