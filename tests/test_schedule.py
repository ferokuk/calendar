"""Обновление календарей расписания."""

import json
from datetime import datetime, timedelta

import pytest

import config
import ruz_schedule
import status
from conftest import LECTURE, SEMINAR, lesson


def today():
    return datetime.now(tz=config.TZ).date()


def day(offset: int) -> str:
    return (today() + timedelta(days=offset)).isoformat()


@pytest.fixture
def fetched(monkeypatch):
    """Подменяет РУЗ: run() получит заданный список занятий."""
    calls = []
    result = []

    def fake_fetch(group_id, start, finish):
        calls.append((start.date(), finish.date()))
        return [dict(item) for item in result]

    monkeypatch.setattr(ruz_schedule, "fetch_schedule", fake_fetch)
    return type("Fetched", (), {"calls": calls, "lessons": result})


def future_lessons(count: int) -> list[dict]:
    return [lesson(day(i + 1), "18:55", "20:25", f"Предмет {i}", SEMINAR) for i in range(count)]


def test_history_window_is_fetched_and_published(paths, monkeypatch, fetched):
    monkeypatch.setattr(config, "SCHEDULE_HISTORY_DAYS", 30, raising=False)
    monkeypatch.setattr(config, "RUZ_DAYS_AHEAD", 60)
    fetched.lessons.append(lesson(day(-5), "18:55", "20:25", "Прошлая лекция", LECTURE))
    fetched.lessons.append(lesson(day(2), "18:55", "20:25", "Будущая лекция", LECTURE))

    ruz_schedule.run()

    assert fetched.calls == [(today() - timedelta(days=30), today() + timedelta(days=60))]
    lectures = (config.CALENDARS_DIR / "lectures.ics").read_text(encoding="utf-8")
    assert "Прошлая лекция" in lectures
    assert "Будущая лекция" in lectures


def test_schedule_json_keeps_lesson_details_for_bot(paths, fetched):
    fetched.lessons.append(lesson(day(1), "18:55", "20:25", "Сети", SEMINAR))

    ruz_schedule.run()

    saved = json.loads(config.SCHEDULE_JSON.read_text(encoding="utf-8"))
    assert saved == [{
        "discipline": "Сети", "date": day(1), "beginLesson": "18:55", "endLesson": "20:25",
        "kindOfWork": SEMINAR, "auditorium": "В4/ауд.2117(кк)",
        "building": "4-й Вешняковский проезд, 4", "group": "ПИ24-2в",
    }]


def test_sharp_drop_keeps_published_calendars(paths, fetched):
    config.SCHEDULE_JSON.write_text(json.dumps(future_lessons(20)), encoding="utf-8")
    lectures = config.CALENDARS_DIR / "lectures.ics"
    lectures.write_text("OLD", encoding="utf-8")

    with pytest.raises(ruz_schedule.ScheduleError):
        ruz_schedule.run()

    assert lectures.read_text(encoding="utf-8") == "OLD"
    assert status.read("schedule")["guard_blocked"] is True


def test_forced_run_publishes_despite_sharp_drop(paths, fetched):
    config.SCHEDULE_JSON.write_text(json.dumps(future_lessons(20)), encoding="utf-8")
    lectures = config.CALENDARS_DIR / "lectures.ics"
    lectures.write_text("OLD", encoding="utf-8")

    ruz_schedule.run(force=True)

    assert lectures.read_text(encoding="utf-8") != "OLD"
    assert status.read("schedule")["guard_blocked"] is False


def test_moderate_change_is_published(paths, fetched):
    config.SCHEDULE_JSON.write_text(json.dumps(future_lessons(20)), encoding="utf-8")
    fetched.lessons.extend(future_lessons(15))

    ruz_schedule.run()

    saved = json.loads(config.SCHEDULE_JSON.read_text(encoding="utf-8"))
    assert len(saved) == 15
