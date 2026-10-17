"""Календари домашки и контрольных."""

from conftest import CHANNEL_ID, bot

import config

HOMEWORK = {
    "kind": "homework", "tag": "модбп", "subject": "Моделирование бизнес-процессов",
    "description": "упр 1", "deadline": "2026-10-29", "chat_id": CHANNEL_ID,
}
CONTROL = {
    "kind": "control", "tag": "модбп", "subject": "Моделирование бизнес-процессов",
    "description": "на листочках", "deadline": "2026-10-29", "pair": 1,
    "start": "18:55", "end": "20:25",
    "location": "В4/ауд.2117(кк), 4-й Вешняковский проезд, 4", "chat_id": CHANNEL_ID,
}
ALL_DAY_CONTROL = {
    "kind": "control", "tag": "модбп", "subject": "Моделирование бизнес-процессов",
    "description": "", "deadline": "2026-10-31", "pair": None,
    "start": None, "end": None, "location": None, "chat_id": CHANNEL_ID,
}


def unfold(ics: str) -> str:
    return ics.replace("\r\n ", "")


def test_controls_share_homework_calendar(paths):
    bot.generate_ics({"10": HOMEWORK, "11": CONTROL})

    ics = config.HOMEWORK_ICS.read_text(encoding="utf-8")
    assert ics.count("BEGIN:VEVENT") == 2
    assert "SUMMARY:ДЗ: Моделирование бизнес-процессов" in ics
    assert "SUMMARY:КР: Моделирование бизнес-процессов" in ics
    assert "X-WR-CALNAME:Домашка" in ics
    assert not (paths.calendars / "controls.ics").exists()


def test_control_event_is_timed_at_lesson_with_room_and_post_link(paths):
    bot.generate_ics({"11": CONTROL})

    ics = unfold(config.HOMEWORK_ICS.read_text(encoding="utf-8"))
    assert "DTSTART:20261029T155500Z" in ics
    assert "DTEND:20261029T172500Z" in ics
    assert "SUMMARY:КР: Моделирование бизнес-процессов" in ics
    assert "LOCATION:В4/ауд.2117(кк)\\, 4-й Вешняковский проезд\\, 4" in ics
    assert "URL:https://t.me/c/2233860778/11" in ics
    assert "TRIGGER:-P1D" in ics


def test_all_day_control_reminds_at_morning_before(paths):
    bot.generate_ics({"12": ALL_DAY_CONTROL})

    ics = config.HOMEWORK_ICS.read_text(encoding="utf-8")
    assert "DTSTART;VALUE=DATE:20261031" in ics
    assert "DTEND;VALUE=DATE:20261101" in ics
    assert "TRIGGER:-PT15H" in ics
