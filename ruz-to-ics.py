"""
Скрипт для генерации .ics файлов из расписания РУЗ ФА.
Разделяет пары по типам (лекции, семинары, экзамены, зачёты) в отдельные календари.
"""

import json
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from hashlib import md5
from pathlib import Path

# ── Настройки ──────────────────────────────────────────────
GROUP_ID = 154832
DAYS_AHEAD = 60  # на сколько дней вперёд
OUTPUT_DIR = Path("/app/calendars")
SUBGROUP_FILTER = "(КАЯиПК)-3"  # подгруппа по английскому; None чтобы отключить

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


def fetch_schedule(group_id: int, start: datetime, finish: datetime) -> list[dict]:
    all_lessons = []
    current = start
    while current <= finish:
        week_end = min(current + timedelta(days=6), finish)
        url = (
            f"https://ruz.fa.ru/api/schedule/group/{group_id}"
            f"?start={current.strftime('%Y.%m.%d')}"
            f"&finish={week_end.strftime('%Y.%m.%d')}&lng=1"
        )
        print(f"  Fetching {current.strftime('%d.%m')} — {week_end.strftime('%d.%m')} ...")
        if all_lessons:
            time.sleep(1)
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            all_lessons.extend(data)
        current = week_end + timedelta(days=1)
    return all_lessons


def filter_subgroups(lessons: list[dict]) -> list[dict]:
    if not SUBGROUP_FILTER:
        return lessons
    result = []
    for lesson in lessons:
        group = lesson.get("group") or ""
        if "(КАЯиПК)" in group:
            if SUBGROUP_FILTER in group:
                result.append(lesson)
        else:
            result.append(lesson)
    return result


def classify(kind_of_work: str) -> str:
    low = kind_of_work.lower()
    for substr, cal in CALENDAR_MAP.items():
        if substr in low:
            return cal
    return DEFAULT_CALENDAR


def make_uid(lesson: dict) -> str:
    raw = f"{lesson['lessonOid']}-{lesson['date']}-{lesson['beginLesson']}"
    return md5(raw.encode()).hexdigest() + "@ruz.fa.ru"


def ics_dt_utc(date_str: str, time_str: str) -> str:
    moscow = timezone(timedelta(hours=3))
    dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M").replace(tzinfo=moscow)
    utc = dt.astimezone(timezone.utc)
    return utc.strftime("%Y%m%dT%H%M%SZ")


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

    for ev in events:
        location_parts = []
        if ev.get("auditorium"):
            location_parts.append(ev["auditorium"])
        if ev.get("building"):
            location_parts.append(ev["building"])
        location = ", ".join(location_parts)

        description_parts = []
        if ev.get("kindOfWork"):
            description_parts.append(ev["kindOfWork"])
        lecturer_name = ev.get("lecturer_title") or ev.get("lecturer")
        if lecturer_name:
            description_parts.append(f"Преподаватель: {lecturer_name}")
        description_parts.append(f"Email: {ev.get('lecturerEmail') or 'нет'}")
        description = "\\n".join(description_parts)

        summary = ev.get("discipline", "Без названия")

        lines.extend([
            "BEGIN:VEVENT",
            f"UID:{make_uid(ev)}",
            f"DTSTAMP:{datetime.now(tz=timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
            f"DTSTART:{ics_dt_utc(ev['date'], ev['beginLesson'])}",
            f"DTEND:{ics_dt_utc(ev['date'], ev['endLesson'])}",
            f"SUMMARY:{escape_ics(summary)}",
            f"LOCATION:{escape_ics(location)}",
            f"DESCRIPTION:{description}",
            "END:VEVENT",
        ])

    lines.append("END:VCALENDAR")
    return "\r\n".join(lines)


def main():
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    finish = today + timedelta(days=DAYS_AHEAD)

    print(f"Загрузка расписания группы {GROUP_ID}")
    print(f"Период: {today.strftime('%d.%m.%Y')} — {finish.strftime('%d.%m.%Y')}\n")

    lessons = fetch_schedule(GROUP_ID, today, finish)
    lessons = filter_subgroups(lessons)
    print(f"\nВсего занятий: {len(lessons)}\n")

    calendars: dict[str, list[dict]] = {}
    for lesson in lessons:
        cal = classify(lesson.get("kindOfWork", ""))
        calendars.setdefault(cal, []).append(lesson)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Сохраняем расписание как JSON для homework-bot
    schedule_data = [
        {"discipline": l["discipline"], "date": l["date"], "beginLesson": l["beginLesson"]}
        for l in lessons
    ]
    schedule_json = OUTPUT_DIR / "schedule.json"
    schedule_json.write_text(json.dumps(schedule_data, ensure_ascii=False), encoding="utf-8")
    print(f"  schedule.json   ({len(schedule_data)} записей)")

    for cal_name in CALENDAR_NAMES:
        events = calendars.get(cal_name, [])
        events.sort(key=lambda e: (e["date"], e["beginLesson"]))
        ics_content = build_ics(cal_name, events)
        out_path = OUTPUT_DIR / f"{cal_name}.ics"
        out_path.write_text(ics_content, encoding="utf-8")
        display_name = CALENDAR_NAMES[cal_name]
        print(f"  {display_name:15s} -> {out_path.name}  ({len(events)} событий)")

    print(f"\nГотово.")


if __name__ == "__main__":
    main()
