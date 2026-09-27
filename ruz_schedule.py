"""
Генерация .ics из расписания РУЗ ФА.

Пары раскладываются по типам (лекции, семинары, экзамены, зачёты) в отдельные
календари. Запускается по крону в своём контейнере и вручную — кнопкой в
админке бота, поэтому вся работа собрана в импортируемой функции run().
"""

import argparse
import http.client
import json
import logging
import os
import random
import socket
import sys
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from hashlib import md5
from pathlib import Path

import config
import logs
import notify
import status

log = logging.getLogger("schedule")

ALERT_KEY = "schedule.update"

# Маппинг kindOfWork → имя календаря.
# Порядок важен: гибридные пары вида "Семинар+зачет" должны попасть в credits,
# поэтому зачёт/экзамен проверяются раньше семинара/лекции.
CALENDAR_MAP = {
    "консульт": "consultations",
    "экзамен":  "exams",
    "зачёт":    "credits",
    "зачет":    "credits",
    "лекци":    "lectures",
    "семинар":  "seminars",
    "практич":  "seminars",
}
DEFAULT_CALENDAR = "other"

CALENDAR_NAMES = {
    "lectures":      "Лекции",
    "seminars":      "Семинары",
    "exams":         "Экзамены",
    "credits":       "Зачёты",
    "consultations": "Консультации",
    "other":         "Прочее",
}


class ScheduleError(RuntimeError):
    """Обновление расписания не удалось; несёт контекст для уведомления."""

    def __init__(self, message: str, *, step: str = "", attempts: int | None = None):
        super().__init__(message)
        self.step = step
        self.attempts = attempts


class InvalidResponseError(ValueError):
    """РУЗ вернул валидный JSON, но не ожидаемый список занятий."""


@contextmanager
def _update_lock(*, blocking: bool = True):
    """Не даёт cron и кнопке админки обновлять одни файлы одновременно."""
    lock_path = config.DATA_DIR / "schedule-update.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock_file:
        try:
            import fcntl
        except ImportError:  # pragma: no cover — локальный запуск на Windows
            fcntl = None
        if fcntl:
            flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
            try:
                fcntl.flock(lock_file.fileno(), flags)
            except BlockingIOError:
                yield False
                return
        try:
            yield True
        finally:
            if fcntl:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


# ── Загрузка ───────────────────────────────────────────────
def _is_retryable(error: BaseException) -> bool:
    """Стоит ли повторять запрос. 4xx (кроме 429) — не стоит, это не наладится."""
    if isinstance(error, urllib.error.HTTPError):
        return error.code == 429 or 500 <= error.code < 600
    return isinstance(error, (
        urllib.error.URLError,
        TimeoutError,
        socket.timeout,
        socket.gaierror,
        ConnectionError,
        http.client.HTTPException,
        json.JSONDecodeError,
        UnicodeDecodeError,
        InvalidResponseError,
    ))


def _backoff_delay(attempt: int) -> float:
    """Экспоненциальная задержка с джиттером, чтобы не долбить сервер в такт."""
    base = min(config.HTTP_BACKOFF * (2 ** (attempt - 1)), config.HTTP_BACKOFF_MAX)
    return round(base + random.uniform(0, base * 0.25), 1)


def _request_json(url: str, label: str) -> list[dict]:
    """GET с ретраями. Бросает ScheduleError, когда попытки исчерпаны."""
    last_error: BaseException | None = None

    for attempt in range(1, config.HTTP_RETRIES + 1):
        try:
            request = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(request, timeout=config.HTTP_TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
                    raise InvalidResponseError("ожидался JSON-массив объектов")
                return data
        except Exception as e:  # noqa: BLE001 — решение о ретрае принимаем ниже
            last_error = e
            reason = notify.describe_error(e)

            if not _is_retryable(e):
                log.error("%s: неустранимая ошибка, повторять бессмысленно — %s", label, reason)
                raise ScheduleError(reason, step=label, attempts=attempt) from e

            if attempt == config.HTTP_RETRIES:
                log.error("%s: не удалось скачать за %d попыток. Последняя ошибка — %s",
                          label, config.HTTP_RETRIES, reason)
                break

            delay = _backoff_delay(attempt)
            log.warning("%s: попытка %d из %d не удалась (%s). Повтор через %.1f с",
                        label, attempt, config.HTTP_RETRIES, reason, delay)
            time.sleep(delay)

    raise ScheduleError(
        notify.describe_error(last_error) if last_error else "неизвестная ошибка",
        step=label,
        attempts=config.HTTP_RETRIES,
    )


def fetch_schedule(group_id: int, start: datetime, finish: datetime) -> list[dict]:
    """Скачивает период по неделям. Любая недобранная неделя рушит весь запуск."""
    all_lessons: list[dict] = []
    current = start
    chunk = 0

    while current <= finish:
        chunk_end = min(current + timedelta(days=config.RUZ_CHUNK_DAYS - 1), finish)
        label = f"неделя {current.strftime('%d.%m')} — {chunk_end.strftime('%d.%m')}"
        url = (
            f"{config.RUZ_API_URL}/{group_id}"
            f"?start={current.strftime('%Y.%m.%d')}"
            f"&finish={chunk_end.strftime('%Y.%m.%d')}&lng=1"
        )

        if chunk and config.FETCH_DELAY:
            time.sleep(config.FETCH_DELAY)
        chunk += 1

        lessons = _request_json(url, label)
        log.info("%s: получено занятий — %d", label, len(lessons))
        all_lessons.extend(lessons)
        current = chunk_end + timedelta(days=1)

    return all_lessons


# ── Преобразование ─────────────────────────────────────────
def classify(kind_of_work: str) -> str:
    low = kind_of_work.lower()
    for substr, cal in CALENDAR_MAP.items():
        if substr in low:
            return cal
    return DEFAULT_CALENDAR


def make_uid(lesson: dict) -> str:
    """Стабильный UID занятия, включая параллельные занятия разных подгрупп."""
    lesson_id = lesson.get("lessonOid") or lesson.get("lessonId")
    group = (lesson.get("group") or "").strip()
    if lesson_id and not group:
        # Сохраняем старые UID там, где нет группового идентификатора.
        raw = f"{lesson_id}-{lesson['date']}-{lesson['beginLesson']}"
    else:
        # РУЗ может повторять lessonOid у разных подгрупп. Группа входит в UID
        # всегда, чтобы исчезновение соседней подгруппы не меняло его.
        # Без lessonOid/lessonId различаем дисциплины по ID или названию:
        # разные предметы в одном слоте не должны поглощать друг друга.
        # Преподаватель и аудитория в идентификатор не входят.
        raw = json.dumps([
            str(lesson_id) if lesson_id else None,
            str(lesson.get("disciplineOid") or lesson.get("disciplineId")
                or lesson.get("discipline") or "")
            if not lesson_id else "",
            lesson["date"], lesson["beginLesson"], group,
        ], ensure_ascii=False, separators=(",", ":"))
    return md5(raw.encode()).hexdigest() + "@ruz.fa.ru"


def ics_dt_utc(date_str: str, time_str: str) -> str:
    dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M").replace(tzinfo=config.TZ)
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def escape_ics(text: str) -> str:
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def build_ics(calendar_name: str, events: list[dict]) -> str:
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//RUZ FA Schedule//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{CALENDAR_NAMES.get(calendar_name, calendar_name)}",
    ]

    seen_uids: set[str] = set()
    for ev in events:
        uid = make_uid(ev)
        if uid in seen_uids:
            continue
        seen_uids.add(uid)
        location_parts = []
        if ev.get("auditorium"):
            location_parts.append(ev["auditorium"])
        if ev.get("building"):
            location_parts.append(ev["building"])
        location = ", ".join(location_parts)

        description_parts = []
        if ev.get("kindOfWork"):
            description_parts.append(ev["kindOfWork"])
        if ev.get("group"):
            description_parts.append(f"Группа: {ev['group']}")
        lecturer_name = ev.get("lecturer_title") or ev.get("lecturer")
        if lecturer_name:
            description_parts.append(f"Преподаватель: {lecturer_name}")
        description_parts.append(f"Email: {ev.get('lecturerEmail') or 'нет'}")
        description = "\n".join(description_parts)

        summary = ev.get("discipline", "Без названия")

        lines.extend([
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{datetime.now(tz=timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
            f"DTSTART:{ics_dt_utc(ev['date'], ev['beginLesson'])}",
            f"DTEND:{ics_dt_utc(ev['date'], ev['endLesson'])}",
            f"SUMMARY:{escape_ics(summary)}",
            f"LOCATION:{escape_ics(location)}",
            f"DESCRIPTION:{escape_ics(description)}",
            "END:VEVENT",
        ])

    lines.append("END:VCALENDAR")
    return "\r\n".join(lines)


def write_atomic(path: Path, content: str) -> None:
    """Пишет через временный файл, чтобы nginx не отдал недописанный календарь."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def publish_files(files: dict[Path, str]) -> None:
    """Сначала готовит все temp-файлы и только затем публикует набор."""
    prepared: list[tuple[Path, Path]] = []
    try:
        for path, content in files.items():
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(content, encoding="utf-8")
            prepared.append((tmp, path))
        for tmp, path in prepared:
            os.replace(tmp, path)
    finally:
        for tmp, _ in prepared:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass


# ── Запуск ─────────────────────────────────────────────────
def _fail(step: str, error: BaseException, attempts: int | None = None) -> ScheduleError:
    """Пишет статус, уведомляет админа и возвращает исключение для raise."""
    reason = notify.describe_error(error)
    previous_failures = status.read("schedule").get("consecutive_failures")
    failures = previous_failures + 1 if isinstance(previous_failures, int) and previous_failures >= 0 else 1
    delay = min(
        config.SCHEDULE_RETRY_SECONDS * (2 ** min(failures - 1, 20)),
        config.SCHEDULE_RETRY_MAX_SECONDS,
    )
    retry_at = (datetime.now(tz=config.TZ) + timedelta(seconds=delay)).isoformat(timespec="seconds")
    status.update(
        "schedule",
        last_error=reason,
        last_error_step=step,
        last_error_at=status.now_iso(),
        last_run_finished=status.now_iso(),
        attempts=attempts,
        in_progress=False,
        consecutive_failures=failures,
        next_retry_at=retry_at,
    )
    log.warning("Обновление не удалось; фоновый повтор после %s (сбой подряд: %d)", retry_at, failures)
    notify.alert(
        "Расписание не обновилось",
        reason,
        step=step,
        attempts=attempts,
        extra={"Группа": config.RUZ_GROUP_ID, "Автоматический повтор после": retry_at},
        key=ALERT_KEY,
    )
    if isinstance(error, ScheduleError):
        return error
    return ScheduleError(reason, step=step, attempts=attempts)


def _run() -> dict:
    """
    Полный цикл обновления. Возвращает сводку, бросает ScheduleError при неудаче
    (статус и уведомление к этому моменту уже записаны).
    """
    started_at = time.monotonic()
    today = datetime.now(tz=config.TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    finish = today + timedelta(days=config.RUZ_DAYS_AHEAD)
    period = f"{today.strftime('%d.%m.%Y')} — {finish.strftime('%d.%m.%Y')}"

    log.info("Обновление расписания: группа %s, период %s", config.RUZ_GROUP_ID, period)
    status.update("schedule", last_run_started=status.now_iso(), in_progress=True, next_retry_at=None)

    try:
        lessons = fetch_schedule(config.RUZ_GROUP_ID, today, finish)
    except ScheduleError as e:
        raise _fail(e.step or "скачивание расписания", e, e.attempts) from e

    unique_lessons: dict[str, dict] = {}
    for lesson in lessons:
        unique_lessons.setdefault(make_uid(lesson), lesson)
    lessons = list(unique_lessons.values())
    log.info("Всего занятий за период: %d", len(lessons))

    calendars: dict[str, list[dict]] = {}
    for lesson in lessons:
        calendars.setdefault(classify(lesson.get("kindOfWork", "")), []).append(lesson)

    # Файлы трогаем только когда весь период выкачан целиком: иначе один
    # таймаут посреди периода вычистил бы уже опубликованный календарь.
    counts: dict[str, int] = {}
    try:
        config.CALENDARS_DIR.mkdir(parents=True, exist_ok=True)

        schedule_data = [
            {"discipline": l["discipline"], "date": l["date"], "beginLesson": l["beginLesson"]}
            for l in lessons
        ]
        output_files = {
            config.SCHEDULE_JSON: json.dumps(schedule_data, ensure_ascii=False),
        }

        for cal_name, display_name in CALENDAR_NAMES.items():
            events = sorted(calendars.get(cal_name, []),
                            key=lambda e: (e["date"], e["beginLesson"]))
            output_files[config.CALENDARS_DIR / f"{cal_name}.ics"] = build_ics(cal_name, events)
            counts[cal_name] = len(events)

        publish_files(output_files)
        log.info("schedule.json: записей — %d", len(schedule_data))
        for cal_name, display_name in CALENDAR_NAMES.items():
            log.info("%-13s → %-18s событий: %d",
                     display_name, cal_name + ".ics", counts[cal_name])
    except (OSError, KeyError, TypeError, ValueError) as e:
        raise _fail("запись файлов календарей", e) from e

    duration = round(time.monotonic() - started_at, 1)
    summary = {
        "lessons": len(lessons),
        "calendars": counts,
        "period": period,
        "group_id": config.RUZ_GROUP_ID,
        "duration_sec": duration,
    }
    status.update(
        "schedule",
        last_success=status.now_iso(),
        last_run_finished=status.now_iso(),
        last_error=None,
        last_error_step=None,
        last_error_at=None,
        attempts=None,
        in_progress=False,
        consecutive_failures=0,
        next_retry_at=None,
        **summary,
    )
    notify.recovered(ALERT_KEY, "Расписание снова обновляется")

    log.info("Готово за %.1f с: занятий %d, непустых календарей %d",
             duration, len(lessons), len([c for c in counts.values() if c]))
    return summary


def _retry_due() -> bool:
    """Проверяется под блокировкой: незавершённый запуск можно безопасно повторить."""
    state = status.read("schedule")
    if state.get("in_progress"):
        return True
    retry_at = state.get("next_retry_at")
    if retry_at:
        try:
            moment = datetime.fromisoformat(retry_at)
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=config.TZ)
            return datetime.now(tz=config.TZ) >= moment
        except (TypeError, ValueError):
            log.warning("Некорректное время фонового повтора: %r", retry_at)
    # Старые статусы ещё не содержат next_retry_at/in_progress.
    started = state.get("last_run_started") or ""
    finished = state.get("last_run_finished") or ""
    unfinished = isinstance(started, str) and isinstance(finished, str) and started > finished
    return bool(state.get("last_error") or not state.get("last_success") or unfinished)


def run(*, retry_only: bool = False, background: bool = False) -> dict | None:
    """Запускает обновление и гарантирует статус/алерт для любой ошибки."""
    try:
        with _update_lock(blocking=not (retry_only or background)) as acquired:
            if not acquired:
                log.info("Обновление уже идёт; фоновый запуск пропущен")
                return None
            if retry_only and not _retry_due():
                return None
            try:
                return _run()
            except ScheduleError:
                raise
            except Exception as e:
                # Состояние ошибки тоже записываем до освобождения блокировки.
                raise _fail("обработка расписания", e) from e
    except ScheduleError:
        raise
    except Exception as e:  # noqa: BLE001 — run() вызывается напрямую из бота
        raise _fail("обработка расписания", e) from e


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Обновление календарей РУЗ")
    parser.add_argument("--retry", action="store_true", help="повторить только неудавшееся обновление, если пора")
    args = parser.parse_args(argv)
    logs.setup("schedule")
    try:
        config.check()
    except config.ConfigError as e:
        log.error("%s", e)
        notify.alert("Расписание не запустилось", e,
                     step="проверка конфигурации", key="schedule.config")
        sys.exit(2)
    notify.recovered("schedule.config", "Конфигурация расписания исправлена")

    try:
        run(retry_only=args.retry, background=True)
    except ScheduleError:
        sys.exit(1)
    except Exception as e:  # noqa: BLE001 — падение крона не должно остаться незамеченным
        log.exception("Непредвиденная ошибка при обновлении расписания")
        _fail("непредвиденная ошибка", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
