"""
Telegram-бот для домашки и контрольных: парсит посты канала, ведёт
homework.ics (задания и контрольные вместе) и держит админку с кнопками
в личке.

Домашка:
    #дз #англ
    упр 14 юнит 6
    до 05.04            (или: до пары, до семинара, до лекции)

Контрольная:
    #кр #модбп
    пишем на листочках
    на 29.10, 1 пара    (или: на 29.10, на паре, на семинаре, на лекции)
"""

import asyncio
import html
import json
import logging
import os
import re
import shutil
import threading
import time
from datetime import date, datetime, time as dtime, timedelta, timezone
from hashlib import md5
from pathlib import Path

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MessageOriginChannel,
    ReactionTypeEmoji,
    ReplyParameters,
    Update,
)
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config
import logs
import notify
import ruz_schedule
import status

log = logging.getLogger("bot")

ALERT_KEY_HOMEWORK = "bot.homework"
ALERT_KEY_RUNTIME = "bot.runtime"
ALERT_KEY_HANDLER = "bot.handler"
ALERT_KEY_STORAGE = "bot.storage"
BOT_FEEDBACK_PREFIX = "⚠️ Бот:"
LEGACY_DEADLINE_NOTE = "⚠️ дедлайн не найден в расписании"

# Теги, которыми заполняется tags.json при первом запуске. Дальше он живёт
# своей жизнью и правится через админку.
DEFAULT_TAGS = {
    "англ": "Иностранный язык",
    "субд": "Системы управления базами данных",
    "матмод": "Математические модели микро- и макроэкономики",
    "сети": "Сетевые системы и приложения",
    "1с": "Экосистема 1С",
    "теоралг": "Теория алгоритмов",
    "стп": "Современные технологии программирования",
    "мо": "Машинное обучение",
}

KIND_NAMES = {"homework": "задание", "control": "контрольная"}

# ── Формат постов ──────────────────────────────────────────
HOMEWORK_MARKER = re.compile(r"(?<!\w)#дз(?!\w)", re.IGNORECASE)
CONTROL_MARKER = re.compile(r"(?<!\w)#кр(?!\w)", re.IGNORECASE)

_DATE = r"(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?"
HOMEWORK_DATE = re.compile(rf"до\s+{_DATE}$", re.IGNORECASE)
# «на 29.10», «на 29.10, 2 пара», «на 29.10 (2-я пара)»
CONTROL_DATE = re.compile(
    rf"(?:на|до)\s+{_DATE}(?:\s*,?\s*\(?\s*(\d)\s*(?:-?я\s*)?пар(?:а|е|у|ы|ой)?\s*\)?)?$",
    re.IGNORECASE,
)

# (шаблон, предпочтительный тип пары, только этот тип)
HOMEWORK_RELATIVE = (
    (re.compile(r"до\s+(?:следующей\s+)?пары$", re.IGNORECASE), "seminar", False),
    (re.compile(r"до\s+(?:следующего\s+)?семинара$", re.IGNORECASE), "seminar", True),
    (re.compile(r"до\s+(?:следующей\s+)?лекции$", re.IGNORECASE), "lecture", True),
)
CONTROL_RELATIVE = (
    (re.compile(r"на\s+(?:следующей\s+)?паре$", re.IGNORECASE), "seminar", False),
    (re.compile(r"на\s+(?:следующем\s+)?семинаре$", re.IGNORECASE), "seminar", True),
    (re.compile(r"на\s+(?:следующей\s+)?лекции$", re.IGNORECASE), "lecture", True),
)

# Строки, похожие на попытку указать дату: о них говорим в ошибке, если
# корректной даты в посте так и не нашлось.
HOMEWORK_DATE_ATTEMPT = re.compile(r"до(?:\s|$)", re.IGNORECASE)
CONTROL_DATE_ATTEMPT = re.compile(r"(?:на|до)\s+(?:\d|след|пар|семинар|лекци)", re.IGNORECASE)

# «до пары» / «на семинаре» и т.п., которые бот заменяет найденной датой
RELATIVE_LINE = re.compile(
    r"(?im)^(до|на)\s+(?:следующ\w+\s+)?(?:пары|паре|семинара|семинаре|лекции)\s*$"
)

# Дата без года, ушедшая в прошлое дальше чем на полгода, относится к
# следующему году: «до 15.01», написанное в декабре.
PAST_DATE_TOLERANCE = timedelta(days=183)

LESSON_KIND_WORDS = {
    "seminar": ("семинар", "практич"),
    "lecture": ("лекци",),
}
LESSON_KIND_PLURAL = {"seminar": "семинаров", "lecture": "лекций", None: "пар"}
LESSON_MINUTES = 90

# Сторож: если цикл бота молчит HEARTBEAT_TIMEOUT секунд или получение
# обновлений из Telegram остановилось, процесс завершается, и Docker
# поднимает его заново (restart: unless-stopped).
HEARTBEAT_INTERVAL = 30
HEARTBEAT_TIMEOUT = 300

DISCUSSION_LIMIT = 500

_homework_lock = asyncio.Lock()
_refresh_task: asyncio.Task | None = None
_heartbeat_at = time.monotonic()
_updater_down = 0
_exit = os._exit


class ScheduleLookupError(RuntimeError):
    """Расписание нельзя использовать для вычисления даты по парам."""


class AmbiguousSubjectError(ScheduleLookupError):
    """Название предмета у тега подходит к нескольким дисциплинам РУЗ."""

    def __init__(self, names: list[str]):
        super().__init__(", ".join(names))
        self.names = names


# ── Хранилище ──────────────────────────────────────────────
def _backup_path(path: Path) -> Path:
    return path.with_name(path.name + ".bak")


def _read_object(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("ожидался JSON-объект")
    return data


def _alert_in_background(*args, **kwargs) -> None:
    """Алерт из синхронного кода, не задерживая цикл бота сетевым запросом."""
    threading.Thread(target=notify.alert, args=args, kwargs=kwargs, daemon=True).start()


def _load_json(path: Path, default):
    """
    Читает JSON-объект. Битый файл откладывается рядом с меткой времени, а
    данные берутся из резервной копии — иначе бот падал бы на каждом
    перезапуске, пока файл не починят руками.
    """
    if not path.exists():
        return default
    try:
        return _read_object(path)
    except ValueError as e:
        error = e

    stamp = datetime.now(tz=config.TZ).strftime("%Y%m%d-%H%M%S")
    quarantine = path.with_name(f"{path.name}.corrupt-{stamp}")
    os.replace(path, quarantine)
    backup = _backup_path(path)
    try:
        data = _read_object(backup)
        shutil.copyfile(backup, path)
        outcome = f"восстановлено из {backup.name}"
    except (OSError, ValueError):
        data = default
        outcome = "резервной копии нет, начато с пустого"
    log.error("%s повреждён (%s): %s; испорченный файл сохранён как %s",
              path.name, notify.describe_error(error), outcome, quarantine.name)
    _alert_in_background(
        f"{path.name} повреждён", error, step="чтение хранилища",
        extra={"Итог": outcome, "Испорченный файл": quarantine.name},
        key=ALERT_KEY_STORAGE, sticky=False,
    )
    return data


def _save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        # Прошлая версия остаётся на случай, если текущую испортят
        shutil.copyfile(path, _backup_path(path))
    ruz_schedule.write_atomic(path, json.dumps(data, ensure_ascii=False, indent=2))


def load_homework() -> dict:
    return _load_json(config.HOMEWORK_JSON, {})


def save_homework(data: dict):
    _save_json(config.HOMEWORK_JSON, data)


def load_tags() -> dict:
    return _load_json(config.TAGS_JSON, DEFAULT_TAGS.copy())


def save_tags(data: dict):
    _save_json(config.TAGS_JSON, data)


def remember_discussion(channel_id: int, post_id: int, chat_id: int, message_id: int) -> None:
    links = _load_json(config.DISCUSSION_JSON, {})
    key = f"{channel_id}:{post_id}"
    links.pop(key, None)
    links[key] = [chat_id, message_id]
    while len(links) > DISCUSSION_LIMIT:
        links.pop(next(iter(links)))
    _save_json(config.DISCUSSION_JSON, links)


def discussion_copy(channel_id: int, post_id: int) -> tuple[int, int] | None:
    """Куда отвечать в группе обсуждения, чтобы попасть в комментарии поста."""
    link = _load_json(config.DISCUSSION_JSON, {}).get(f"{channel_id}:{post_id}")
    if isinstance(link, list) and len(link) == 2:
        return int(link[0]), int(link[1])
    return None


# ── Ретеншен ───────────────────────────────────────────────
def prune_homework(homework: dict, retention_days: int | None = None) -> int:
    """
    Физически удаляет задания с дедлайном старше HOMEWORK_RETENTION_DAYS.

    Раньше старьё только пряталось при генерации .ics, а сам homework.json рос
    без конца. Возвращает количество удалённых записей.
    """
    days = config.HOMEWORK_RETENTION_DAYS if retention_days is None else retention_days
    cutoff = (datetime.now(tz=config.TZ) - timedelta(days=days)).strftime("%Y-%m-%d")
    stale = [key for key, hw in homework.items()
             if hw.get("deadline") and hw["deadline"] < cutoff]
    for key in stale:
        del homework[key]
    if stale:
        log.info("Ретеншен: удалено заданий с дедлайном раньше %s — %d", cutoff, len(stale))
    return len(stale)


def persist_homework(homework: dict, step: str, *, record_activity: bool = True) -> bool:
    """Чистит, сохраняет и перегенерирует .ics. При ошибке уведомляет админа."""
    try:
        prune_homework(homework)
        save_homework(homework)
        generate_ics(homework)
    except Exception as e:  # noqa: BLE001 — потеря домашки должна быть видна сразу
        log.exception("Не удалось сохранить домашку (%s)", step)
        status.update(
            "bot",
            last_error=notify.describe_error(e),
            last_error_at=status.now_iso(),
            last_error_source="homework",
        )
        notify.alert("Домашка не сохранена", e, step=step, key=ALERT_KEY_HOMEWORK)
        return False

    notify.recovered(ALERT_KEY_HOMEWORK, "Домашка снова сохраняется")
    fields = {"homework_active": count_active(homework)}
    if record_activity:
        fields["last_homework_at"] = status.now_iso()
    bot_status = status.read("bot")
    if bot_status.get("last_error_source") == "homework":
        fields.update(last_error=None, last_error_at=None, last_error_source=None)
    status.update("bot", **fields)
    return True


async def persist(homework: dict, step: str, *, record_activity: bool = True) -> bool:
    """persist_homework вне цикла бота: запись файлов и алерты не тормозят обработку."""
    return await asyncio.to_thread(
        persist_homework, homework, step, record_activity=record_activity
    )


# ── Расписание ─────────────────────────────────────────────
def load_schedule() -> list[dict]:
    if not config.SCHEDULE_JSON.exists():
        log.warning("Расписание %s ещё не сгенерировано", config.SCHEDULE_JSON.name)
        raise ScheduleLookupError("расписание ещё не загружено")
    try:
        schedule = json.loads(config.SCHEDULE_JSON.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.warning("Расписание %s недоступно: %s",
                    config.SCHEDULE_JSON.name, notify.describe_error(e))
        raise ScheduleLookupError("файл расписания недоступен") from e
    if not isinstance(schedule, list) or not all(
        isinstance(lesson, dict)
        and isinstance(lesson.get("date"), str)
        and isinstance(lesson.get("discipline"), str)
        for lesson in schedule
    ):
        raise ScheduleLookupError("некорректный формат расписания")
    return schedule


def _normalize(name: str) -> str:
    return " ".join(name.casefold().replace("ё", "е").split())


def subject_lessons(subject: str, schedule: list[dict]) -> list[dict]:
    """
    Занятия предмета. Название у тега может быть короче, чем в РУЗ:
    «Иностранный язык» находит «Иностранный язык в профессиональной сфере».
    """
    wanted = _normalize(subject)
    exact = [lesson for lesson in schedule if _normalize(lesson["discipline"]) == wanted]
    if exact:
        return exact
    prefixed = [lesson for lesson in schedule
                if _normalize(lesson["discipline"]).startswith(wanted + " ")]
    names = sorted({lesson["discipline"] for lesson in prefixed})
    if len(names) > 1:
        raise AmbiguousSubjectError(names)
    return prefixed


def lesson_kind(lesson: dict) -> str | None:
    """«seminar», «lecture» или None. «Семинар+зачет» — тоже семинар."""
    low = (lesson.get("kindOfWork") or "").lower()
    for kind, words in LESSON_KIND_WORDS.items():
        if any(word in low for word in words):
            return kind
    return None


def find_next_lesson(subject: str, after: date, kind: str | None,
                     strict: bool) -> dict | None:
    """
    Первое занятие предмета после дня `after`. Занятия типа `kind` идут в
    приоритете; при `strict` другие типы не подходят вовсе.
    """
    later = sorted(
        (lesson for lesson in subject_lessons(subject, load_schedule())
         if lesson["date"] > after.isoformat()),
        key=lambda lesson: (lesson["date"], lesson.get("beginLesson") or ""),
    )
    if kind:
        preferred = [lesson for lesson in later if lesson_kind(lesson) == kind]
        if preferred or strict:
            later = preferred
    return later[0] if later else None


def pair_slots(subject: str, day: date) -> list[dict]:
    """
    Пары предмета в этот день по порядку. Параллельные подгруппы в одно время —
    одна пара; аудитория остаётся, только если она у всех одна.
    """
    lessons = [lesson for lesson in subject_lessons(subject, load_schedule())
               if lesson["date"] == day.isoformat() and lesson.get("beginLesson")]
    slots: dict[str, dict] = {}
    for lesson in sorted(lessons, key=lambda lesson: lesson["beginLesson"]):
        location = ruz_schedule.lesson_location(lesson) or None
        slot = slots.setdefault(lesson["beginLesson"], {
            "start": lesson["beginLesson"],
            "end": lesson.get("endLesson"),
            "location": location,
        })
        if slot["location"] != location:
            slot["location"] = None
    return list(slots.values())


# ── Парсер постов ──────────────────────────────────────────
def strip_bot_feedback(text: str) -> str:
    """Удаляет нашу служебную ошибку перед повторным разбором поста."""
    lines = [
        line for line in text.splitlines()
        if not line.strip().startswith(BOT_FEEDBACK_PREFIX)
    ]
    cleaned = "\n".join(lines)
    # Совместимость с первой версией пометки, которая дописывалась в строку.
    cleaned = re.sub(
        rf"(?im)\s+—\s+{re.escape(LEGACY_DEADLINE_NOTE)}\s*$",
        "",
        cleaned,
    )
    return cleaned.rstrip()


def post_kind(text: str) -> str | None:
    """
    «homework», «control», «conflict» (оба маркера) или None — пост не для бота.

    Маркер ищется только в первой строке: так задуман формат, а анонсы и
    обсуждения, где #дз/#кр упоминаются в тексте, бот не трогает.
    """
    first_line = next((line for line in strip_bot_feedback(text).splitlines() if line.strip()), "")
    homework = bool(HOMEWORK_MARKER.search(first_line))
    control = bool(CONTROL_MARKER.search(first_line))
    if homework and control:
        return "conflict"
    if control:
        return "control"
    if homework:
        return "homework"
    return None


def _tags_hint(tags: dict) -> str:
    shown = [f"#{tag}" for tag in sorted(tags)[:12]]
    suffix = " …" if len(tags) > len(shown) else ""
    return f" Доступные теги: {', '.join(shown)}{suffix}." if shown else ""


def resolve_date(day: int, month: int, year: int | None, anchor: date) -> date:
    """Дата из поста; год без явного указания выводится от дня публикации."""
    if year:
        return date(year, month, day)
    try:
        candidate = date(anchor.year, month, day)
    except ValueError:
        # 29.02 в невисокосный год подходит только следующему году
        return date(anchor.year + 1, month, day)
    if candidate < anchor - PAST_DATE_TOLERANCE:
        return date(anchor.year + 1, month, day)
    return candidate


def _pairs_word(count: int) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return "пара"
    if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        return "пары"
    return "пар"


def _ambiguous_message(error: AmbiguousSubjectError) -> str:
    return (f"тег подходит сразу к нескольким предметам: {', '.join(error.names)}. "
            "Уточните название предмета у тега в админке.")


def _next_lesson(subject: str, posted_on: date, kind: str, strict: bool,
                 *, what: str, manual: str) -> tuple[dict | None, str | None]:
    """Ближайшая пара для «до пары» / «на паре» или текст ошибки для поста."""
    try:
        lesson = find_next_lesson(subject, posted_on, kind, strict)
    except AmbiguousSubjectError as e:
        return None, _ambiguous_message(e)
    except ScheduleLookupError:
        return None, (
            f"расписание временно недоступно, поэтому вычислить {what} нельзя. "
            f"Укажите дату вручную: {manual}."
        )
    if not lesson:
        plural = LESSON_KIND_PLURAL[kind if strict else None]
        log.warning("Для предмета «%s» не нашлось будущих %s в расписании", subject, plural)
        return None, (
            f"в расписании нет будущих {plural} по предмету «{subject}». "
            f"Укажите дату вручную: {manual}."
        )
    return lesson, None


def _parse_homework_deadline(lines: list[str], subject: str,
                             posted_on: date) -> tuple[int | None, dict | None, str | None]:
    """Строка дедлайна домашки → (номер строки, поля записи, ошибка)."""
    attempt = None
    for i, line in enumerate(lines):
        m = HOMEWORK_DATE.match(line)
        if m:
            try:
                deadline = resolve_date(int(m.group(1)), int(m.group(2)),
                                        int(m.group(3)) if m.group(3) else None, posted_on)
            except ValueError:
                log.warning("Некорректный дедлайн в посте: %s", line)
                return None, None, (
                    f"дата «{line}» не существует. Укажите корректную дату: "
                    "до ДД.ММ или до ДД.ММ.ГГГГ."
                )
            return i, {"deadline": deadline.isoformat(), "needs_date_replace": False}, None

        for pattern, kind, strict in HOMEWORK_RELATIVE:
            if pattern.match(line):
                lesson, error = _next_lesson(subject, posted_on, kind, strict,
                                             what="дедлайн", manual="до ДД.ММ")
                if error:
                    return None, None, error
                return i, {"deadline": lesson["date"], "needs_date_replace": True}, None

        if attempt is None and HOMEWORK_DATE_ATTEMPT.match(line):
            attempt = line

    if attempt:
        log.warning("Неверный формат дедлайна в посте: %s", attempt)
        return None, None, (
            f"неверный формат дедлайна «{attempt}». Используйте: до 05.09, "
            "до 05.09.2026, до пары, до семинара или до лекции."
        )
    log.warning("Дедлайн не найден в посте: %s", lines[0][:60])
    return None, None, "не указан дедлайн. Добавьте отдельную строку: до 05.09 или до пары."


def _control_slot(subject: str, day: date, pair: int | None,
                  begin: str | None = None) -> tuple[dict | None, str | None]:
    """Время и аудитория контрольной. Без пары в этот день — событие на весь день."""
    fields = {"deadline": day.isoformat(), "pair": pair,
              "start": None, "end": None, "location": None}
    try:
        slots = pair_slots(subject, day)
    except AmbiguousSubjectError as e:
        return None, _ambiguous_message(e)
    except ScheduleLookupError:
        log.warning("Расписание недоступно — контрольная %s будет на весь день", day)
        return fields, None

    if begin:
        index = next((i for i, s in enumerate(slots) if s["start"] == begin), None)
        slot = slots[index] if index is not None else None
        # Номер пары нужен, чтобы «на паре» → «на 29.10, 2 пара» читалось так же
        fields["pair"] = index + 1 if index is not None else None
    elif not slots:
        slot = None
    elif pair is None:
        slot = slots[0]
    elif 1 <= pair <= len(slots):
        slot = slots[pair - 1]
    else:
        return None, (
            f"{day.strftime('%d.%m')} у предмета «{subject}» только {len(slots)} "
            f"{_pairs_word(len(slots))}. Укажите номер пары от 1 до {len(slots)}."
        )

    if slot:
        end = slot["end"] or (
            datetime.strptime(slot["start"], "%H:%M") + timedelta(minutes=LESSON_MINUTES)
        ).strftime("%H:%M")
        fields.update(start=slot["start"], end=end, location=slot["location"])
    return fields, None


def _parse_control_date(lines: list[str], subject: str,
                        posted_on: date) -> tuple[int | None, dict | None, str | None]:
    """Строка даты контрольной → (номер строки, поля записи, ошибка)."""
    attempt = None
    for i, line in enumerate(lines):
        m = CONTROL_DATE.match(line)
        if m:
            try:
                day = resolve_date(int(m.group(1)), int(m.group(2)),
                                   int(m.group(3)) if m.group(3) else None, posted_on)
            except ValueError:
                log.warning("Некорректная дата контрольной в посте: %s", line)
                return None, None, (
                    f"дата «{line}» не существует. Укажите корректную дату: "
                    "на ДД.ММ или на ДД.ММ.ГГГГ."
                )
            pair = int(m.group(4)) if m.group(4) else None
            fields, error = _control_slot(subject, day, pair)
            if error:
                return None, None, error
            return i, {**fields, "needs_date_replace": False}, None

        for pattern, kind, strict in CONTROL_RELATIVE:
            if pattern.match(line):
                lesson, error = _next_lesson(subject, posted_on, kind, strict,
                                             what="дату", manual="на ДД.ММ")
                if error:
                    return None, None, error
                fields, error = _control_slot(subject, date.fromisoformat(lesson["date"]),
                                              None, begin=lesson.get("beginLesson"))
                if error:
                    return None, None, error
                return i, {**fields, "needs_date_replace": True}, None

        if attempt is None and CONTROL_DATE_ATTEMPT.match(line):
            attempt = line

    if attempt:
        log.warning("Неверный формат даты контрольной в посте: %s", attempt)
        return None, None, (
            f"неверный формат даты «{attempt}». Используйте: на 29.10, "
            "на 29.10, 2 пара, на паре, на семинаре или на лекции."
        )
    log.warning("Дата контрольной не найдена в посте: %s", lines[0][:60])
    return None, None, (
        "не указана дата контрольной. Добавьте отдельную строку: "
        "на 29.10, на 29.10, 2 пара или на паре."
    )


def parse_post_detailed(text: str, *,
                        posted_on: date | None = None) -> tuple[dict | None, str | None]:
    """
    Парсит пост и возвращает понятную пользователю причину отказа.

    `posted_on` — день публикации: от него считаются год у даты без года и
    «до пары». У правки поста это по-прежнему день исходной публикации.
    """
    posted_on = posted_on or datetime.now(tz=config.TZ).date()
    text = strip_bot_feedback(text)
    kind = post_kind(text) or "homework"
    if kind == "conflict":
        return None, (
            "в посте одновременно #дз и #кр. Оставьте один маркер: "
            "#дз — домашка, #кр — контрольная."
        )
    control = kind == "control"
    marker = "#кр" if control else "#дз"
    # Убираем маркер перед парсингом
    text = re.sub(rf"{(CONTROL_MARKER if control else HOMEWORK_MARKER).pattern}\s*", "",
                  text, flags=re.IGNORECASE)
    lines = [line.strip() for line in text.strip().split("\n") if line.strip()]
    if not lines:
        if control:
            return None, (
                "после #кр не хватает данных. Добавьте тег предмета и дату, "
                "например: #кр #сети / тема / на 29.10."
            )
        return None, (
            "после #дз не хватает данных. Добавьте тег предмета, задание "
            "и дедлайн, например: #дз #англ / упражнение 5 / до 05.09."
        )

    tag_match = re.search(r"#(\S+)", lines[0])
    if not tag_match:
        tags = load_tags()
        return None, (
            f"не указан тег предмета в первой строке. Например: {marker} #англ."
            + _tags_hint(tags)
        )
    tag = tag_match.group(1).lower()
    tags = load_tags()
    subject = tags.get(tag)
    if not subject:
        log.warning("Неизвестный тег #%s — пост пропущен", tag)
        return None, (
            f"предмет с тегом #{tag} не найден. Проверьте тег или попросите "
            "администратора добавить его." + _tags_hint(tags)
        )

    parse_date = _parse_control_date if control else _parse_homework_deadline
    date_line_idx, fields, error = parse_date(lines, subject, posted_on)
    if error:
        return None, error

    desc_lines = []
    for i, line in enumerate(lines):
        if i == 0:
            rest = re.sub(r"#\S+\s*", "", line).strip()
            if rest:
                desc_lines.append(rest)
        elif i != date_line_idx:
            desc_lines.append(line)
    description = "\n".join(desc_lines)
    # У контрольной описание необязательно: сам факт и есть новость
    if not description and not control:
        return None, (
            "не указано само задание. Добавьте описание между тегом предмета "
            "и строкой дедлайна."
        )

    return {
        "kind": kind,
        "tag": tag,
        "subject": subject,
        "description": description,
        **fields,
    }, None


def parse_post(text: str) -> dict | None:
    """Совместимый интерфейс парсера для вызовов без диагностики."""
    parsed, _ = parse_post_detailed(text)
    return parsed


def with_bot_feedback(text: str, error: str) -> str:
    """Добавляет или заменяет служебную строку с ошибкой."""
    source = strip_bot_feedback(text)
    return f"{source}\n\n{BOT_FEEDBACK_PREFIX} {error}"


# ── ICS генератор ─────────────────────────────────────────
def is_control(hw: dict) -> bool:
    return hw.get("kind") == "control"


def post_url(chat_id, message_id) -> str | None:
    """Ссылка на пост канала; t.me/c/… открывается у его подписчиков."""
    chat = str(chat_id or "")
    if not chat.startswith("-100"):
        return None
    return f"https://t.me/c/{chat[4:]}/{message_id}"


def _dtstamp() -> str:
    return f"DTSTAMP:{datetime.now(tz=timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"


def _homework_event(msg_id: str, hw: dict) -> list[str]:
    uid = md5(f"hw-{msg_id}".encode()).hexdigest() + "@homework.bot"
    dt = hw["deadline"].replace("-", "")
    end = datetime.strptime(hw["deadline"], "%Y-%m-%d") + timedelta(days=1)
    return [
        "BEGIN:VEVENT",
        f"UID:{uid}",
        _dtstamp(),
        f"DTSTART;VALUE=DATE:{dt}",
        f"DTEND;VALUE=DATE:{end.strftime('%Y%m%d')}",
        f"SUMMARY:ДЗ: {ruz_schedule.escape_ics(hw['subject'])}",
        f"DESCRIPTION:{ruz_schedule.escape_ics(hw['description'])}",
        "BEGIN:VALARM",
        "TRIGGER:-P1D",
        "ACTION:DISPLAY",
        "DESCRIPTION:Дедлайн завтра",
        "END:VALARM",
        "END:VEVENT",
    ]


def _control_event(msg_id: str, hw: dict) -> list[str]:
    uid = md5(f"kr-{msg_id}".encode()).hexdigest() + "@homework.bot"
    url = post_url(hw.get("chat_id"), msg_id)
    if hw.get("start") and hw.get("end"):
        when = [
            f"DTSTART:{ruz_schedule.ics_dt_utc(hw['deadline'], hw['start'])}",
            f"DTEND:{ruz_schedule.ics_dt_utc(hw['deadline'], hw['end'])}",
        ]
        trigger = "-P1D"  # за сутки до начала пары
    else:
        day = datetime.strptime(hw["deadline"], "%Y-%m-%d")
        when = [
            f"DTSTART;VALUE=DATE:{day.strftime('%Y%m%d')}",
            f"DTEND;VALUE=DATE:{(day + timedelta(days=1)).strftime('%Y%m%d')}",
        ]
        trigger = "-PT15H"  # событие на весь день начинается в 00:00 → 09:00 накануне

    description = "\n\n".join(
        part for part in (hw.get("description"), f"Пост: {url}" if url else None) if part
    )
    lines = [
        "BEGIN:VEVENT",
        f"UID:{uid}",
        _dtstamp(),
        *when,
        f"SUMMARY:КР: {ruz_schedule.escape_ics(hw['subject'])}",
    ]
    if hw.get("location"):
        lines.append(f"LOCATION:{ruz_schedule.escape_ics(hw['location'])}")
    if description:
        lines.append(f"DESCRIPTION:{ruz_schedule.escape_ics(description)}")
    if url:
        lines.append(f"URL:{url}")
    lines.extend([
        "BEGIN:VALARM",
        f"TRIGGER:{trigger}",
        "ACTION:DISPLAY",
        "DESCRIPTION:Контрольная завтра",
        "END:VALARM",
        "END:VEVENT",
    ])
    return lines


def generate_ics(homework: dict):
    """
    Собирает homework.ics: задания и контрольные в одном календаре.
    Отсев старья делает prune_homework до вызова.
    """
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Homework Bot//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:Домашка",
    ]

    controls = 0
    for msg_id, hw in homework.items():
        if is_control(hw):
            lines.extend(_control_event(msg_id, hw))
            controls += 1
        else:
            lines.extend(_homework_event(msg_id, hw))

    lines.append("END:VCALENDAR")

    config.HOMEWORK_ICS.parent.mkdir(parents=True, exist_ok=True)
    ruz_schedule.write_atomic(config.HOMEWORK_ICS, "\r\n".join(lines))
    log.info("homework.ics обновлён: заданий — %d, контрольных — %d",
             len(homework) - controls, controls)


# ── Реакции ────────────────────────────────────────────────
async def react(post, emoji: str | None):
    """Ставит реакцию на пост канала; None снимает реакцию бота."""
    try:
        await post.set_reaction([ReactionTypeEmoji(emoji)] if emoji else [])
    except Exception as e:  # noqa: BLE001 — реакция необязательна, ронять обработку нельзя
        log.warning("Не удалось поставить реакцию %s: %s", repr(emoji), notify.describe_error(e))


def post_content(post) -> str:
    """Текст обычного поста или подпись поста с вложением."""
    text = getattr(post, "text", None)
    return text if text is not None else (getattr(post, "caption", None) or "")


def post_html(post) -> str:
    """То же с форматированием в HTML — чтобы правка поста его не стирала."""
    if getattr(post, "text", None) is not None:
        return post.text_html
    if getattr(post, "caption", None) is not None:
        return post.caption_html
    return ""


async def edit_post_content(post, content: str) -> None:
    """Редактирует HTML-текст, не удаляя прикреплённый к media-посту файл."""
    if getattr(post, "text", None) is not None:
        options = {}
        if getattr(post, "link_preview_options", None) is not None:
            options["link_preview_options"] = post.link_preview_options
        await post.edit_text(content, parse_mode="HTML", **options)
    else:
        await post.edit_caption(caption=content, parse_mode="HTML")


async def reject_post(post, error: str) -> None:
    """Пишет понятную ошибку прямо в пост и ставит отрицательную реакцию."""
    current_text = post_html(post)
    marked_text = with_bot_feedback(current_text, html.escape(error))
    if marked_text != current_text:
        try:
            await edit_post_content(post, marked_text)
            log.info("Пост %s помечен пользовательской ошибкой: %s", post.message_id, error)
        except Exception as e:  # noqa: BLE001 — реакцию всё равно нужно поставить
            log.warning("Не удалось добавить пометку в пост %s: %s",
                        post.message_id, notify.describe_error(e))
    await react(post, "👎")


async def normalize_valid_post(post, parsed: dict) -> None:
    """После исправления удаляет ошибку и подставляет дату вместо «до пары»."""
    current_text = post_html(post)
    new_text = strip_bot_feedback(current_text)
    if parsed.get("needs_date_replace"):
        day = datetime.strptime(parsed["deadline"], "%Y-%m-%d").strftime("%d.%m")
        if is_control(parsed) and (parsed.get("pair") or 1) > 1:
            day += f", {parsed['pair']} пара"
        new_text = RELATIVE_LINE.sub(lambda m: f"{m.group(1)} {day}", new_text, count=1)
    if new_text == current_text:
        return
    try:
        await edit_post_content(post, new_text)
    except Exception as e:  # noqa: BLE001 — данные уже сохранены, правка косметическая
        log.warning("Не удалось очистить пометку в посте %s: %s",
                    post.message_id, notify.describe_error(e))


# ── Форматирование для админки ─────────────────────────────
def esc(value) -> str:
    return html.escape(str(value))


def fmt_deadline(deadline: str) -> str:
    """«2026-09-05» → «05.09, через 3 дн.»"""
    try:
        day = datetime.strptime(deadline, "%Y-%m-%d").date()
    except ValueError:
        return deadline
    left = (day - datetime.now(tz=config.TZ).date()).days
    if left < 0:
        note = f"просрочено на {-left} дн."
    elif left == 0:
        note = "сегодня"
    elif left == 1:
        note = "завтра"
    else:
        note = f"через {left} дн."
    return f"{day.strftime('%d.%m')}, {note}"


def tag_hash(tag: str) -> str:
    """Короткий идентификатор тега для callback_data (64 байта — потолок)."""
    return md5(tag.encode("utf-8")).hexdigest()[:8]


def active_homework() -> list[tuple[str, dict]]:
    homework = load_homework()
    today = datetime.now(tz=config.TZ).strftime("%Y-%m-%d")
    active = [item for item in homework.items() if item[1].get("deadline", "") >= today]
    return sorted(active, key=lambda item: (item[1].get("deadline", ""), item[1].get("start") or ""))


def count_active(homework: dict) -> int:
    today = datetime.now(tz=config.TZ).strftime("%Y-%m-%d")
    return sum(hw.get("deadline", "") >= today for hw in homework.values())


def channel_line(bot_status: dict) -> str:
    if config.HOMEWORK_CHANNEL_ID is not None:
        return f"<code>{esc(config.HOMEWORK_CHANNEL_ID)}</code>"
    seen = bot_status.get("last_channel_id")
    hint = (f", последний пост из <code>{esc(seen)}</code> "
            f"({esc(bot_status.get('last_channel_title') or '—')})") if seen else ""
    return f"⚠️ любой — задай HOMEWORK_CHANNEL_ID{hint}"


# ── Экраны админки ─────────────────────────────────────────
def screen_root() -> tuple[str, InlineKeyboardMarkup]:
    sched = status.read("schedule")
    homework_count = len(active_homework())

    if sched.get("last_error"):
        sched_line = f"🔴 сбой: {esc(sched['last_error'][:120])}"
    elif sched.get("last_success"):
        sched_line = f"✅ {esc(status.human_time(sched.get('last_success')))}"
    else:
        sched_line = "— ещё не запускалось"

    text = (
        "<b>⚙️ Админка</b>\n\n"
        f"🗓 Расписание: {sched_line}\n"
        f"📚 Домашка и контрольные: активных — {homework_count}"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🏷 Теги", callback_data="nav:tags"),
         InlineKeyboardButton("📚 Домашка", callback_data="nav:hw")],
        [InlineKeyboardButton("🗓 Расписание", callback_data="nav:sched"),
         InlineKeyboardButton("📊 Статус", callback_data="nav:status")],
    ])
    return text, keyboard


def screen_tags() -> tuple[str, InlineKeyboardMarkup]:
    tags = load_tags()
    if tags:
        body = "\n".join(f"<code>#{esc(k)}</code> — {esc(v)}" for k, v in sorted(tags.items()))
    else:
        body = "<i>Пусто. Добавь первый тег кнопкой ниже.</i>"

    rows = []
    pair = []
    for tag in sorted(tags):
        pair.append(InlineKeyboardButton(f"❌ #{tag}", callback_data=f"tag:del:{tag_hash(tag)}"))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows.append([InlineKeyboardButton("➕ Добавить", callback_data="tag:add")])
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data="nav:root")])

    text = f"<b>🏷 Теги ({len(tags)})</b>\n\n{body}\n\nКнопка ❌ удаляет тег сразу."
    return text, InlineKeyboardMarkup(rows)


def screen_homework() -> tuple[str, InlineKeyboardMarkup]:
    items = active_homework()
    if items:
        blocks = []
        for _, hw in items[:20]:
            desc = hw.get("description", "").strip().replace("\n", " ")
            if len(desc) > 90:
                desc = desc[:90] + "…"
            title = f"КР · {hw['subject']}" if is_control(hw) else hw["subject"]
            when = fmt_deadline(hw["deadline"]) + (f", {hw['start']}" if hw.get("start") else "")
            blocks.append(
                f"• <b>{esc(title)}</b> — {esc(when)}\n"
                f"  {esc(desc) if desc else '<i>без описания</i>'}"
            )
        body = "\n".join(blocks)
        if len(items) > 20:
            body += f"\n\n<i>И ещё: {len(items) - 20}</i>"
    else:
        body = "<i>Активных заданий и контрольных нет.</i>"

    rows = []
    for msg_id, hw in items[:20]:
        prefix = "КР " if is_control(hw) else ""
        label = f"❌ {prefix}{hw['subject'][:20]} · {hw['deadline'][8:10]}.{hw['deadline'][5:7]}"
        rows.append([InlineKeyboardButton(label, callback_data=f"hw:del:{msg_id}")])
    rows.append([InlineKeyboardButton("🧹 Очистить просроченные", callback_data="hw:prune")])
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data="nav:root")])

    text = (
        f"<b>📚 Домашка и контрольные — {len(items)} шт.</b>\n\n{body}\n\n"
        f"<i>Хранение: {config.HOMEWORK_RETENTION_DAYS} дн. после даты.</i>"
    )
    return text, InlineKeyboardMarkup(rows)


def screen_schedule() -> tuple[str, InlineKeyboardMarkup]:
    sched = status.read("schedule")
    counts = sched.get("calendars") or {}
    by_calendar = "\n".join(
        f"  {esc(ruz_schedule.CALENDAR_NAMES.get(name, name))}: {count}"
        for name, count in counts.items() if count
    ) or "  <i>нет данных</i>"

    lines = [
        "<b>🗓 Расписание</b>\n",
        f"Группа: <code>{esc(config.RUZ_GROUP_ID)}</code>",
        f"Глубина: {config.SCHEDULE_HISTORY_DAYS} дн. назад, {config.RUZ_DAYS_AHEAD} дн. вперёд",
        f"Период: {esc(sched.get('period', '—'))}",
        f"Обновление по крону: <code>{esc(config.SCHEDULE_CRON)}</code>",
        "",
        f"Последний успех: {esc(status.human_time(sched.get('last_success')))}",
        f"Занятий: {sched.get('lessons', '—')}",
        by_calendar,
    ]
    if sched.get("last_error"):
        lines += [
            "",
            f"🔴 Последняя ошибка ({esc(status.human_time(sched.get('last_error_at')))})",
            f"Шаг: {esc(sched.get('last_error_step', '—'))}",
            f"<code>{esc(sched['last_error'])}</code>",
        ]
    if sched.get("guard_blocked"):
        lines += [
            "",
            f"⚠️ Публикация остановлена: занятий впереди было {esc(sched.get('guard_before'))}, "
            f"пришло {esc(sched.get('guard_after'))}. Если так и должно быть — "
            "опубликуй кнопкой ниже.",
        ]
    if sched.get("next_retry_at"):
        lines.append(f"🔄 Автоповтор после: {esc(status.human_time(sched['next_retry_at']))}")

    rows = [[InlineKeyboardButton("🔄 Обновить сейчас", callback_data="sched:refresh")]]
    if sched.get("guard_blocked"):
        rows.append([InlineKeyboardButton("⚠️ Опубликовать как есть", callback_data="sched:force")])
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data="nav:root")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def screen_status() -> tuple[str, InlineKeyboardMarkup]:
    sched = status.read("schedule")
    bot = status.read("bot")

    files = []
    if config.CALENDARS_DIR.exists():
        for path in sorted(config.CALENDARS_DIR.glob("*.ics")):
            stat = path.stat()
            when = datetime.fromtimestamp(stat.st_mtime, tz=config.TZ).strftime("%d.%m %H:%M")
            files.append(f"  {esc(path.name)} — {stat.st_size // 1024} КБ, {when}")
    files_block = "\n".join(files) or "  <i>файлов нет</i>"

    bot_error = esc(bot.get("last_error") or "нет")
    if bot.get("last_error") and bot.get("last_error_at"):
        bot_error += f" ({esc(status.human_time(bot['last_error_at']))})"

    lines = [
        "<b>📊 Статус</b>\n",
        "<b>Расписание</b>",
        f"  Успех: {esc(status.human_time(sched.get('last_success')))}",
        f"  Длительность: {sched.get('duration_sec', '—')} с",
        f"  Ошибка: {esc(sched.get('last_error') or 'нет')}",
        "",
        "<b>Бот</b>",
        f"  Запущен: {esc(status.human_time(bot.get('started_at')))}",
        f"  Канал: {channel_line(bot)}",
        f"  Активных ДЗ и КР: {bot.get('homework_active', count_active(load_homework()))}",
        f"  Последнее задание: {esc(status.human_time(bot.get('last_homework_at')))}",
        f"  Последняя чистка: {esc(status.human_time(bot.get('last_cleanup')))}",
        f"  Последняя ошибка: {bot_error}",
        "",
        "<b>Файлы</b>",
        files_block,
        "",
        "<b>Настройки</b>",
        f"  Ретеншен: {config.HOMEWORK_RETENTION_DAYS} дн., чистка в "
        f"{config.HOMEWORK_CLEANUP_HOUR:02d}:00",
        f"  Ретраи: {config.HTTP_RETRIES}, таймаут {config.HTTP_TIMEOUT:.0f} с",
        f"  Уведомления: {'вкл' if config.ALERTS_ENABLED else 'выкл'}, "
        f"кулдаун {config.ALERT_COOLDOWN_MINUTES} мин",
    ]

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Обновить экран", callback_data="nav:status")],
        [InlineKeyboardButton("◀️ Назад", callback_data="nav:root")],
    ])
    return "\n".join(lines), keyboard


SCREENS = {
    "root": screen_root,
    "tags": screen_tags,
    "hw": screen_homework,
    "sched": screen_schedule,
    "status": screen_status,
}


# ── Админка ────────────────────────────────────────────────
def is_admin(update: Update) -> bool:
    user = update.effective_user
    return bool(user and user.id == config.ADMIN_CHAT_ID)


def _is_stale_query(error: object) -> bool:
    text = str(error).lower()
    return isinstance(error, BadRequest) and (
        "query is too old" in text or "query id is invalid" in text
    )


async def answer(query, text: str | None = None, **kwargs) -> None:
    """Отвечает на нажатие кнопки. Устаревшее нажатие ошибкой не считается."""
    try:
        await query.answer(text, **kwargs)
    except BadRequest as e:
        if not _is_stale_query(e):
            raise
        log.info("Ответ на нажатие кнопки пропущен: нажатие устарело")


async def show(update: Update, screen: str, notice: str | None = None):
    """Рисует экран: правит текущее сообщение при клике, шлёт новое при команде."""
    text, keyboard = SCREENS[screen]()
    if notice:
        text = f"{notice}\n\n{text}"

    query = update.callback_query
    if query:
        try:
            await query.edit_message_text(text, reply_markup=keyboard, parse_mode="HTML")
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                raise
    else:
        await update.effective_message.reply_text(
            text, reply_markup=keyboard, parse_mode="HTML"
        )


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/start и /menu — открыть админку."""
    if not is_admin(update):
        return
    context.user_data.pop("awaiting", None)
    await show(update, "root")


async def start_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE, *, force: bool):
    """
    Запускает обновление расписания в фоне. При сбоях РУЗ оно идёт минутами,
    а обработчик, ждущий его до конца, держал бы очередь: остальные сообщения
    стояли бы, а нажатия кнопок успевали устареть.
    """
    global _refresh_task
    query = update.callback_query
    if _refresh_task and not _refresh_task.done():
        await answer(query, "Обновление уже идёт", show_alert=True)
        return
    await answer(query, "Запустил, итог появится в этом сообщении")
    try:
        await query.edit_message_text("🔄 Обновляю расписание…", parse_mode="HTML")
    except BadRequest:
        pass
    _refresh_task = context.application.create_task(
        _refresh(update, force=force), update=update, name="schedule-refresh"
    )


async def _refresh(update: Update, *, force: bool):
    try:
        summary = await asyncio.to_thread(ruz_schedule.run, force=force)
        if summary:
            notice = (f"✅ Обновлено: занятий {summary['lessons']} "
                      f"за {summary['duration_sec']} с")
        else:
            notice = "ℹ️ Расписание как раз обновлялось по крону — показаны свежие данные"
    except Exception as e:  # noqa: BLE001 — текст ошибки нужен на экране
        notice = (f"🔴 Не удалось: {esc(notify.describe_error(e))}\n"
                  "Обновление повторится автоматически в фоне.")
    try:
        await show(update, "sched", notice=notice)
    except Exception as e:  # noqa: BLE001 — сообщение могли удалить, обновление уже прошло
        log.warning("Не удалось показать итог обновления: %s", notify.describe_error(e))


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(update):
        await answer(query, "Недоступно", show_alert=True)
        return

    data = query.data or ""

    if data.startswith("nav:"):
        await answer(query)
        await show(update, data.split(":", 1)[1])
        return

    if data == "tag:add":
        context.user_data["awaiting"] = "tag"
        await answer(query)
        await query.edit_message_text(
            "➕ <b>Новый тег</b>\n\n"
            "Пришли одним сообщением: <code>тег Название предмета</code>\n"
            "Например: <code>линал Линейная алгебра</code>\n\n"
            "Отмена — /menu",
            parse_mode="HTML",
        )
        return

    if data.startswith("tag:del:"):
        wanted = data.split(":", 2)[2]
        tags = load_tags()
        target = next((t for t in tags if tag_hash(t) == wanted), None)
        if not target:
            await answer(query, "Тег уже удалён", show_alert=True)
        else:
            subject = tags.pop(target)
            save_tags(tags)
            log.info("Удалён тег #%s (%s)", target, subject)
            await answer(query, f"Удалён #{target}")
        await show(update, "tags")
        return

    if data.startswith("hw:del:"):
        msg_id = data.split(":", 2)[2]
        async with _homework_lock:
            homework = load_homework()
            removed = homework.pop(msg_id, None)
            if not removed:
                await answer(query, "Задание уже удалено", show_alert=True)
            else:
                if await persist(homework, "удаление задания из админки"):
                    log.info("Удалено задание %s (%s)", msg_id, removed.get("subject"))
                    await answer(query, f"Удалено: {removed.get('subject', '')}"[:200])
                else:
                    await answer(query, "Не удалось сохранить", show_alert=True)
        await show(update, "hw")
        return

    if data == "hw:prune":
        async with _homework_lock:
            homework = load_homework()
            before = len(homework)
            prune_homework(homework, retention_days=0)
            if await persist(homework, "ручная очистка просроченного"):
                await answer(query, f"Удалено: {before - len(homework)}")
            else:
                await answer(query, "Не удалось сохранить", show_alert=True)
        await show(update, "hw")
        return

    if data in ("sched:refresh", "sched:force"):
        await start_refresh(update, context, force=data == "sched:force")
        return

    await answer(query)


async def on_private_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ловит ввод для сценариев админки (сейчас — добавление тега)."""
    if not is_admin(update):
        return
    if context.user_data.get("awaiting") != "tag":
        await show(update, "root")
        return

    context.user_data.pop("awaiting", None)
    parts = (update.message.text or "").strip().split(maxsplit=1)
    if len(parts) < 2:
        await update.message.reply_text(
            "Нужно два поля: тег и название.\nНапример: <code>линал Линейная алгебра</code>",
            parse_mode="HTML",
        )
        await show(update, "tags")
        return

    tag = parts[0].lower().lstrip("#")
    subject = parts[1].strip()
    tags = load_tags()
    tags[tag] = subject
    save_tags(tags)
    log.info("Добавлен тег #%s — %s", tag, subject)
    await show(update, "tags", notice=f"✅ Добавлен <code>#{esc(tag)}</code> — {esc(subject)}")


# ── Команды (остались рабочими помимо кнопок) ─────────────
async def cmd_tags(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать все теги: /tags"""
    if not is_admin(update):
        return
    await show(update, "tags")


async def cmd_addtag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/addtag тег Название предмета"""
    if not is_admin(update):
        return
    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "Формат: /addtag тег Название предмета\nПример: /addtag линал Линейная алгебра"
        )
        return
    tag = context.args[0].lower().lstrip("#")
    subject = " ".join(context.args[1:])
    tags = load_tags()
    tags[tag] = subject
    save_tags(tags)
    log.info("Добавлен тег #%s — %s", tag, subject)
    await show(update, "tags", notice=f"✅ Добавлен <code>#{esc(tag)}</code> — {esc(subject)}")


async def cmd_deltag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/deltag тег"""
    if not is_admin(update):
        return
    if not context.args:
        await update.message.reply_text("Формат: /deltag тег\nПример: /deltag линал")
        return
    tag = context.args[0].lower().lstrip("#")
    tags = load_tags()
    if tag not in tags:
        await update.message.reply_text(f"Тег #{tag} не найден")
        return
    subject = tags.pop(tag)
    save_tags(tags)
    log.info("Удалён тег #%s (%s)", tag, subject)
    await show(update, "tags", notice=f"🗑 Удалён <code>#{esc(tag)}</code> — {esc(subject)}")


# ── Обработчики канала ─────────────────────────────────────
def channel_allowed(chat_id: int) -> bool:
    return config.HOMEWORK_CHANNEL_ID is None or chat_id == config.HOMEWORK_CHANNEL_ID


def post_day(post) -> date:
    """День публикации в нашей таймзоне. У правки это день исходного поста."""
    moment = getattr(post, "date", None)
    if moment is None:
        return datetime.now(tz=config.TZ).date()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(config.TZ).date()


async def accept_channel(post) -> bool:
    """Пост из нашего канала? Без HOMEWORK_CHANNEL_ID запоминает, откуда он пришёл."""
    if not channel_allowed(post.chat_id):
        log.info("Пост %s из чужого канала %s пропущен", post.message_id, post.chat_id)
        return False
    if config.HOMEWORK_CHANNEL_ID is None:
        await asyncio.to_thread(
            status.update, "bot",
            last_channel_id=post.chat_id,
            last_channel_title=getattr(post.chat, "title", None),
        )
    return True


def same_record(old: dict, new: dict) -> bool:
    """Совпадает ли суть записи — так отсекается собственная правка «до пары» → дата."""
    def view(record: dict) -> tuple:
        return (record.get("kind", "homework"),) + tuple(
            record.get(field)
            for field in ("tag", "deadline", "description", "start", "end", "location")
        )
    return view(old) == view(new)


async def comment_on_post(context: ContextTypes.DEFAULT_TYPE, post, text: str) -> None:
    """Пишет в комментарии к посту — ответом на его копию в группе обсуждения."""
    link = await asyncio.to_thread(discussion_copy, post.chat_id, post.message_id)
    if not link:
        log.info("Комментарий к посту %s не отправлен: копия поста в обсуждении неизвестна",
                 post.message_id)
        return
    chat_id, message_id = link
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_parameters=ReplyParameters(message_id=message_id),
        )
    except Exception as e:  # noqa: BLE001 — комментарий необязателен
        log.info("Комментарий к посту %s не отправлен: %s",
                 post.message_id, notify.describe_error(e))


async def handle_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    post = update.channel_post
    if not post:
        return
    content = post_content(post)
    if not content or not post_kind(content):
        return
    if not await accept_channel(post):
        return

    parsed, parse_error = parse_post_detailed(content, posted_on=post_day(post))
    if not parsed:
        await reject_post(post, parse_error or "не удалось разобрать сообщение.")
        return
    parsed["chat_id"] = post.chat_id

    msg_id = str(post.message_id)
    async with _homework_lock:
        homework = load_homework()
        homework[msg_id] = parsed
        if not await persist(homework, f"новый пост #{parsed['tag']}"):
            await react(post, "👎")
            return

    await normalize_valid_post(post, parsed)

    await react(post, "👍")
    log.info("Принято: %s #%s (%s) на %s: %s", KIND_NAMES[parsed["kind"]],
             parsed["tag"], parsed["subject"], parsed["deadline"],
             parsed["description"][:50])


async def handle_edited_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    post = update.edited_channel_post
    if not post:
        return
    content = post_content(post)
    if not content or not channel_allowed(post.chat_id):
        return

    msg_id = str(post.message_id)
    if not post_kind(content):
        async with _homework_lock:
            homework = load_homework()
            old = homework.get(msg_id)
            if old:
                del homework[msg_id]
                if not await persist(homework, f"снятие маркера с поста {msg_id}"):
                    await react(post, "👎")
                    return
                await react(post, "👌")
                log.info("Запись %s удалена — из поста убран #дз/#кр", msg_id)
                return
        # Пост не для бота, но несёт нашу старую пометку об ошибке — убираем её и 👎
        if BOT_FEEDBACK_PREFIX in content:
            cleaned = strip_bot_feedback(post_html(post))
            try:
                await edit_post_content(post, cleaned)
            except Exception as e:  # noqa: BLE001 — пометка косметическая
                log.warning("Не удалось убрать пометку из поста %s: %s",
                            post.message_id, notify.describe_error(e))
            await react(post, None)
            log.info("Пост %s больше не для бота — пометка и реакция сняты", msg_id)
        return

    parsed, parse_error = parse_post_detailed(content, posted_on=post_day(post))
    if not parsed:
        async with _homework_lock:
            homework = load_homework()
            if homework.pop(msg_id, None):
                if not await persist(homework, f"невалидная правка поста {msg_id}"):
                    await react(post, "👎")
                    return
                log.info("Запись %s удалена из календаря после невалидной правки", msg_id)
        await reject_post(post, parse_error or "не удалось разобрать сообщение.")
        return
    parsed["chat_id"] = post.chat_id

    async with _homework_lock:
        homework = load_homework()
        old = homework.get(msg_id)
        unchanged = bool(old and same_record(old, parsed))
        if not unchanged:
            homework[msg_id] = parsed
            if not await persist(homework, f"правка поста {msg_id}"):
                await react(post, "👎")
                return

    await normalize_valid_post(post, parsed)

    # Реакция показывает текущее состояние поста: после исправления ошибки
    # заменяем прежний 👎 на 👍. set_reaction заменяет реакцию этого бота.
    await react(post, "👍")

    if unchanged:
        log.debug("Запись %s: данные не изменились, служебная пометка очищена", msg_id)
        return

    changes = []
    if old:
        old_kind = old.get("kind", "homework")
        if old_kind != parsed["kind"]:
            changes.append(f"тип: {KIND_NAMES[old_kind]} → {KIND_NAMES[parsed['kind']]}")
        if old.get("deadline") != parsed["deadline"]:
            label = "дата" if is_control(parsed) else "дедлайн"
            changes.append(f"{label}: {old['deadline']} → {parsed['deadline']}")
        if old.get("start") != parsed.get("start"):
            changes.append(f"время: {old.get('start') or 'весь день'} → "
                           f"{parsed.get('start') or 'весь день'}")
        if old.get("description") != parsed["description"]:
            changes.append("описание обновлено")
        if old.get("tag") != parsed["tag"]:
            changes.append(f"предмет: #{old['tag']} → #{parsed['tag']}")

    if changes:
        now = datetime.now(tz=config.TZ).strftime("%d.%m %H:%M")
        comment = f"✏️ Изменения учтены ({now})\n" + "\n".join(f"• {c}" for c in changes)
        await comment_on_post(context, post, comment)

    log.info("Изменено: %s #%s (%s) на %s", KIND_NAMES[parsed["kind"]], parsed["tag"],
             parsed["subject"], parsed["deadline"])


async def remember_discussion_copy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Запоминает копию поста канала в группе обсуждения. У копии свой id, и
    комментарий к посту — это ответ именно на неё, а не на id поста в канале.
    """
    message = update.message
    origin = getattr(message, "forward_origin", None)
    if (not message or not message.is_automatic_forward
            or not isinstance(origin, MessageOriginChannel)
            or not channel_allowed(origin.chat.id)):
        return
    await asyncio.to_thread(
        remember_discussion, origin.chat.id, origin.message_id,
        message.chat_id, message.message_id,
    )


# ── Фон и ошибки ───────────────────────────────────────────
async def daily_cleanup(context: ContextTypes.DEFAULT_TYPE):
    """
    Ежедневная чистка. Нужна отдельно от обработчиков: без неё просроченное
    задание висело бы в календаре до следующего поста в канале.
    """
    async with _homework_lock:
        homework = load_homework()
        removed = prune_homework(homework)
        if not await persist(homework, "ежедневный ретеншен", record_activity=False):
            return

    await asyncio.to_thread(status.update, "bot", last_cleanup=status.now_iso(),
                            homework_active=count_active(homework))
    log.info("Ежедневная чистка: удалено %d, осталось %d", removed, len(homework))


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    """
    Ошибка обработки одного обновления — в лог и разовым уведомлением. Бот при
    этом продолжает работать, поэтому состояния «сломан» она не заводит.
    """
    error = context.error
    if _is_stale_query(error):
        log.info("Устаревшее нажатие кнопки: %s", error)
        return
    exc_info = ((type(error), error, error.__traceback__)
                if isinstance(error, BaseException) else None)
    log.error("Необработанная ошибка обработчика", exc_info=exc_info)
    await asyncio.to_thread(
        status.update,
        "bot",
        last_error=notify.describe_error(error),
        last_error_at=status.now_iso(),
        last_error_source="handler",
    )

    where = "обработка обновления"
    if isinstance(update, Update) and update.effective_message:
        where = f"сообщение {update.effective_message.message_id}"
    await asyncio.to_thread(
        notify.alert,
        "Ошибка при обработке обновления",
        error or "неизвестная ошибка",
        step=where,
        extra={"Бот": "работает, остальные сообщения обрабатываются"},
        key=ALERT_KEY_HANDLER,
        sticky=False,
    )


def _restart(reason: str) -> None:
    """Завершает процесс, чтобы Docker поднял бота заново."""
    log.critical("%s — перезапускаю процесс", reason)
    sender = threading.Thread(
        target=notify.alert, args=("Бот перезапускается", reason),
        kwargs={"step": "сторож", "key": ALERT_KEY_RUNTIME}, daemon=True,
    )
    sender.start()
    sender.join(timeout=20)
    _exit(1)


async def heartbeat(context: ContextTypes.DEFAULT_TYPE):
    """Отметка «цикл жив» для сторожа и проверка, что polling не остановился."""
    global _heartbeat_at, _updater_down
    _heartbeat_at = time.monotonic()
    application = context.application
    updater = getattr(application, "updater", None)
    # При штатной остановке updater гасится раньше приложения, поэтому
    # перезапуск — только если polling стоит две проверки подряд.
    if updater is not None and application.running and not updater.running:
        _updater_down += 1
        if _updater_down >= 2:
            await asyncio.to_thread(_restart, "получение сообщений из Telegram остановилось")
    else:
        _updater_down = 0


def _watch_event_loop() -> None:
    """Отдельный поток: замечает зависший цикл бота, которого Docker не видит."""
    while True:
        time.sleep(HEARTBEAT_INTERVAL)
        silent = time.monotonic() - _heartbeat_at
        if silent > HEARTBEAT_TIMEOUT:
            _restart(f"бот не отвечает {int(silent)} с")
            return


async def on_startup(app: Application):
    global _heartbeat_at
    await asyncio.to_thread(
        status.update,
        "bot",
        started_at=status.now_iso(),
        last_error=None,
        last_error_at=None,
        last_error_source=None,
    )
    if not config.TAGS_JSON.exists():
        save_tags(DEFAULT_TAGS.copy())
    homework = load_homework()
    removed = prune_homework(homework)
    if not await persist(homework, "инициализация хранилища", record_activity=False):
        raise RuntimeError("не удалось инициализировать хранилище домашки")
    await asyncio.to_thread(notify.recovered, ALERT_KEY_RUNTIME, "Бот снова работает")
    if config.HOMEWORK_CHANNEL_ID is None:
        log.warning("HOMEWORK_CHANNEL_ID не задан: #дз и #кр принимаются из любого канала, "
                    "где бот админ")
    if app.job_queue:
        _heartbeat_at = time.monotonic()
        threading.Thread(target=_watch_event_loop, name="watchdog", daemon=True).start()
    log.info("Бот запущен. Активных записей: %d, всего хранится: %d, админ: %s",
             count_active(homework), len(homework), config.ADMIN_CHAT_ID)
    if removed:
        log.info("При запуске удалено по ретеншену: %d", removed)


# ── Запуск ─────────────────────────────────────────────────
def main():
    logs.setup("bot")
    try:
        config.check()
    except config.ConfigError as e:
        log.error("%s", e)
        notify.alert("Бот не запустился", e,
                     step="проверка конфигурации", key="bot.config")
        raise SystemExit(2) from e
    notify.recovered("bot.config", "Конфигурация бота исправлена")

    app = Application.builder().token(config.TG_BOT_TOKEN).post_init(on_startup).build()

    app.add_handler(CommandHandler(["start", "menu"], cmd_menu))
    app.add_handler(CommandHandler("tags", cmd_tags))
    app.add_handler(CommandHandler("addtag", cmd_addtag))
    app.add_handler(CommandHandler("deltag", cmd_deltag))
    app.add_handler(CallbackQueryHandler(on_callback))

    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, on_private_text
    ))
    app.add_handler(MessageHandler(filters.IS_AUTOMATIC_FORWARD, remember_discussion_copy))
    app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POST, handle_channel_post))
    app.add_handler(MessageHandler(filters.UpdateType.EDITED_CHANNEL_POST, handle_edited_post))

    app.add_error_handler(on_error)

    if app.job_queue:
        app.job_queue.run_daily(
            daily_cleanup,
            time=dtime(hour=config.HOMEWORK_CLEANUP_HOUR, minute=0, tzinfo=config.TZ),
            name="homework-retention",
        )
        app.job_queue.run_repeating(
            heartbeat, interval=HEARTBEAT_INTERVAL, first=HEARTBEAT_INTERVAL, name="heartbeat",
        )
        log.info("Чистка домашки запланирована на %02d:00 каждый день",
                 config.HOMEWORK_CLEANUP_HOUR)
    else:
        log.warning("JobQueue недоступна — ежедневная чистка и сторож не запущены. "
                    "Проверь, что установлен python-telegram-bot[job-queue]")

    try:
        app.run_polling(allowed_updates=[
            "channel_post", "edited_channel_post", "message", "callback_query",
        ])
    except Exception as e:  # noqa: BLE001 — ошибки жизненного цикла не идут в error handler
        log.exception("Бот аварийно остановился")
        status.update(
            "bot",
            last_error=notify.describe_error(e),
            last_error_at=status.now_iso(),
            last_error_source="runtime",
        )
        notify.alert("Бот аварийно остановился", e, step="polling", key=ALERT_KEY_RUNTIME)
        raise


if __name__ == "__main__":
    main()
