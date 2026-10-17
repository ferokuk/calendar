"""Разбор постов канала: домашка (#дз) и контрольные (#кр)."""

from datetime import date

from conftest import bot

TUESDAY = date(2026, 10, 27)


def parse(text, posted_on=TUESDAY):
    return bot.parse_post_detailed(text, posted_on=posted_on)


# ── Домашка ────────────────────────────────────────────────
def test_until_lesson_targets_next_seminar_not_lecture(schedule):
    parsed, error = parse("#дз #модбп\nупр 1\nдо пары")

    assert error is None
    assert parsed["deadline"] == "2026-10-29"
    assert parsed["needs_date_replace"] is True


def test_until_lecture_targets_next_lecture(schedule):
    parsed, error = parse("#дз #модбп\nупр 1\nдо лекции")

    assert error is None
    assert parsed["deadline"] == "2026-10-28"


def test_until_lesson_falls_back_to_lecture_when_subject_has_no_seminars(schedule):
    parsed, error = parse("#дз #цп\nпрочитать главу\nдо пары")

    assert error is None
    assert parsed["deadline"] == "2026-10-30"


def test_until_seminar_is_rejected_when_subject_has_no_seminars(schedule):
    parsed, error = parse("#дз #цп\nпрочитать главу\nдо семинара")

    assert parsed is None
    assert "семинар" in error


def test_tag_subject_matches_longer_discipline_name(schedule):
    parsed, error = parse("#дз #англ\nэссе\nдо пары")

    assert error is None
    assert parsed["deadline"] == "2026-11-03"


def test_date_without_year_in_december_moves_to_next_year(schedule):
    parsed, error = parse("#дз #модбп\nэссе\nдо 15.01", posted_on=date(2026, 12, 20))

    assert error is None
    assert parsed["deadline"] == "2027-01-15"


def test_recently_passed_date_keeps_current_year(schedule):
    parsed, error = parse("#дз #модбп\nэссе\nдо 15.10", posted_on=date(2026, 10, 17))

    assert error is None
    assert parsed["deadline"] == "2026-10-15"


def test_description_line_starting_with_do_does_not_hide_valid_deadline(schedule):
    parsed, error = parse("#дз #модбп\nдо конца главы 3 дочитать\nдо 29.10")

    assert error is None
    assert parsed["deadline"] == "2026-10-29"
    assert parsed["description"] == "до конца главы 3 дочитать"


# ── Контрольные ────────────────────────────────────────────
def test_control_on_date_and_pair_uses_lesson_time_and_room(schedule):
    parsed, error = parse(
        "#кр #модбп\n"
        "пишем на листочках, подробности: https://t.me/c/2233860778/11524/12118\n"
        "на 29.10, 1 пара"
    )

    assert error is None
    assert parsed["kind"] == "control"
    assert parsed["subject"] == "Моделирование бизнес-процессов"
    assert parsed["deadline"] == "2026-10-29"
    assert (parsed["start"], parsed["end"]) == ("18:55", "20:25")
    assert parsed["location"] == "В4/ауд.2117(кк), 4-й Вешняковский проезд, 4"
    assert parsed["description"] == (
        "пишем на листочках, подробности: https://t.me/c/2233860778/11524/12118"
    )


def test_control_second_pair(schedule):
    parsed, error = parse("#кр #модбп\nтест\nна 29.10, 2 пара")

    assert error is None
    assert (parsed["start"], parsed["end"]) == ("20:30", "22:00")


def test_control_on_date_defaults_to_first_pair(schedule):
    parsed, error = parse("#кр #модбп\nтест\nна 29.10")

    assert error is None
    assert parsed["start"] == "18:55"


def test_control_on_lesson_targets_next_seminar(schedule):
    parsed, error = parse("#кр #модбп\nтест\nна паре")

    assert error is None
    assert parsed["deadline"] == "2026-10-29"
    assert parsed["start"] == "18:55"
    assert parsed["needs_date_replace"] is True


def test_control_on_day_without_subject_lessons_is_all_day(schedule):
    parsed, error = parse("#кр #модбп\nтест\nна 31.10")

    assert error is None
    assert parsed["deadline"] == "2026-10-31"
    assert parsed["start"] is None


def test_control_pair_number_beyond_day_lessons_is_rejected(schedule):
    parsed, error = parse("#кр #модбп\nтест\nна 29.10, 3 пара")

    assert parsed is None
    assert "2" in error


def test_control_without_description_is_accepted(schedule):
    parsed, error = parse("#кр #модбп\nна 29.10")

    assert error is None
    assert parsed["description"] == ""


def test_control_description_may_start_with_na(schedule):
    parsed, error = parse("#кр #модбп\nна листочках\nна 29.10")

    assert error is None
    assert parsed["description"] == "на листочках"


def test_control_parallel_subgroups_count_as_one_pair_without_room(schedule):
    parsed, error = parse("#кр #англ\nлексика\nна 03.11")

    assert error is None
    assert parsed["start"] == "18:55"
    assert parsed["location"] is None


def test_post_with_both_markers_is_rejected(schedule):
    parsed, error = parse("#дз #кр #модбп\nтест\nна 29.10")

    assert parsed is None
    assert "#дз" in error and "#кр" in error


# ── Распознавание типа поста ───────────────────────────────
def test_post_kind_detects_markers():
    assert bot.post_kind("#дз #англ\nупр\nдо 05.11") == "homework"
    assert bot.post_kind("#кр #англ\nна 05.11") == "control"
    assert bot.post_kind("#дзшка в чате") is None


def test_post_kind_ignores_marker_inside_bot_feedback():
    text = "просто пост\n\n⚠️ Бот: не указан тег. Например: #дз #англ."

    assert bot.post_kind(text) is None
