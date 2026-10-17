"""
Общая обвязка тестов.

config.py читает окружение при импорте, поэтому переменные задаются до
импорта модулей проекта. Файлы каждого теста живут в своём tmp_path.
"""

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
_SESSION_DIR = Path(tempfile.mkdtemp(prefix="ruz-calendar-tests-"))

os.environ.update({
    "TG_BOT_TOKEN": "123456:test-token",
    "ADMIN_CHAT_ID": "1",
    "RUZ_GROUP_ID": "164386",
    "DATA_DIR": str(_SESSION_DIR / "data"),
    "CALENDARS_DIR": str(_SESSION_DIR / "calendars"),
    "ALERTS_ENABLED": "false",
    "FETCH_DELAY": "0",
    "TZ": "Europe/Moscow",
    "TZ_LABEL": "MSK",
})
sys.path.insert(0, str(ROOT))

import config  # noqa: E402


def _load_bot_module():
    spec = importlib.util.spec_from_file_location("homework_bot", ROOT / "homework-bot.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["homework_bot"] = module
    spec.loader.exec_module(module)
    return module


bot = _load_bot_module()

CHANNEL_ID = -1002233860778
BUILDING = "4-й Вешняковский проезд, 4"
SEMINAR = "Практические (семинарские) занятия"
LECTURE = "Лекции"


def lesson(date, begin, end, discipline, kind, auditorium="В4/ауд.2117(кк)", group="ПИ24-2в"):
    return {
        "discipline": discipline,
        "date": date,
        "beginLesson": begin,
        "endLesson": end,
        "kindOfWork": kind,
        "auditorium": auditorium,
        "building": BUILDING,
        "group": group,
    }


# Неделя группы ПИ24-2в из РУЗ: у МодБП в среду лекция, в четверг семинар,
# по две пары подряд; английский идёт параллельными подгруппами.
SCHEDULE = [
    lesson("2026-10-27", "18:55", "20:25", "Иностранный язык в профессиональной сфере",
           SEMINAR, "В4/ауд.1502(кк)", "005344_3 Иностранный язык (КАЯиПК)-1"),
    lesson("2026-10-28", "18:55", "20:25", "Моделирование бизнес-процессов", LECTURE, "В4/3305", None),
    lesson("2026-10-28", "20:30", "22:00", "Моделирование бизнес-процессов", LECTURE, "В4/3305", None),
    lesson("2026-10-29", "18:55", "20:25", "Моделирование бизнес-процессов", SEMINAR),
    lesson("2026-10-29", "20:30", "22:00", "Моделирование бизнес-процессов", SEMINAR),
    lesson("2026-10-30", "18:55", "20:25", "Цифровые платформы", LECTURE, "В4/3305", None),
    lesson("2026-10-29", "17:20", "18:50", "Проектирование информационных систем",
           LECTURE, "В4/3305", None),
    lesson("2026-10-29", "18:55", "20:25", "Проектирование информационных систем", SEMINAR),
    lesson("2026-11-03", "18:55", "20:25", "Иностранный язык в профессиональной сфере",
           SEMINAR, "В4/ауд.1502(кк)", "005344_3 Иностранный язык (КАЯиПК)-1"),
    lesson("2026-11-03", "18:55", "20:25", "Иностранный язык в профессиональной сфере",
           SEMINAR, "В4/ауд.3202(кк)", "005344_3 Иностранный язык (КАЯиПК)-2"),
]

TAGS = {
    "модбп": "Моделирование бизнес-процессов",
    "англ": "Иностранный язык",
    "цп": "Цифровые платформы",
    "пис": "Проектирование информационных систем",
}


@pytest.fixture
def paths(tmp_path, monkeypatch):
    """Перенаправляет все файлы проекта во временный каталог теста."""
    data = tmp_path / "data"
    calendars = tmp_path / "calendars"
    data.mkdir()
    calendars.mkdir()
    for name, value in {
        "DATA_DIR": data,
        "CALENDARS_DIR": calendars,
        "HOMEWORK_JSON": data / "homework.json",
        "TAGS_JSON": data / "tags.json",
        "ALERTS_JSON": data / "alerts.json",
        "STATUS_JSON": data / "status.json",
        "DISCUSSION_JSON": data / "discussion.json",
        "SCHEDULE_JSON": calendars / "schedule.json",
        "HOMEWORK_ICS": calendars / "homework.ics",
    }.items():
        monkeypatch.setattr(config, name, value, raising=False)
    return SimpleNamespace(data=data, calendars=calendars)


@pytest.fixture
def schedule(paths):
    """Расписание и теги, как их видит бот."""
    config.SCHEDULE_JSON.write_text(json.dumps(SCHEDULE, ensure_ascii=False), encoding="utf-8")
    config.TAGS_JSON.write_text(json.dumps(TAGS, ensure_ascii=False), encoding="utf-8")
    return SCHEDULE
