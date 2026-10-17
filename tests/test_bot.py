"""Обработчики бота: правка постов, канал, обсуждение, админка, сторож, хранилище."""

import asyncio
import json
import threading
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from telegram import Chat, MessageOriginChannel
from telegram.error import BadRequest

import config
import ruz_schedule
from conftest import CHANNEL_ID, bot

POSTED_AT = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)


class FakePost:
    def __init__(self, text, *, html=None, chat_id=CHANNEL_ID, message_id=10):
        self.text = text
        self.text_html = html if html is not None else text
        self.caption = None
        self.caption_html = None
        self.chat_id = chat_id
        self.chat = SimpleNamespace(id=chat_id, title="Канал группы")
        self.message_id = message_id
        self.date = POSTED_AT
        self.link_preview_options = None
        self.edits = []
        self.reactions = []

    async def edit_text(self, text, **kwargs):
        self.edits.append((text, kwargs))

    async def edit_caption(self, caption=None, **kwargs):
        self.edits.append((caption, kwargs))

    async def set_reaction(self, reactions):
        self.reactions.append(reactions[0].emoji if reactions else None)


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)

    async def get_chat(self, chat_id):
        raise AssertionError("связь с обсуждением должна браться из запомненной копии")


class FakeQuery:
    def __init__(self, data, *, answer_error=None):
        self.data = data
        self.answer_error = answer_error
        self.answers = []
        self.edits = []

    async def answer(self, text=None, **kwargs):
        if self.answer_error:
            raise self.answer_error
        self.answers.append(text)

    async def edit_message_text(self, text, **kwargs):
        self.edits.append(text)


def admin_update(query):
    return SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=config.ADMIN_CHAT_ID))


def admin_context():
    loop = asyncio.get_running_loop()
    application = SimpleNamespace(
        create_task=lambda coroutine, update=None, name=None: loop.create_task(coroutine)
    )
    return SimpleNamespace(application=application, user_data={}, bot=FakeBot())


def saved_homework():
    if not config.HOMEWORK_JSON.exists():
        return {}
    return json.loads(config.HOMEWORK_JSON.read_text(encoding="utf-8"))


# ── Правка постов ──────────────────────────────────────────
def test_rejected_post_keeps_formatting(paths):
    post = FakePost("#дз #модбп\nупр 1", html="#дз #модбп\n<b>упр 1</b>")

    asyncio.run(bot.reject_post(post, "дата <1> не существует"))

    text, kwargs = post.edits[0]
    assert text.startswith("#дз #модбп\n<b>упр 1</b>")
    assert "дата &lt;1&gt; не существует" in text
    assert kwargs["parse_mode"] == "HTML"


def test_relative_date_replacement_keeps_formatting(paths):
    post = FakePost("#кр #модбп\nупр\nна паре", html="#кр #модбп\n<i>упр</i>\nна паре")
    parsed = {"deadline": "2026-10-29", "needs_date_replace": True}

    asyncio.run(bot.normalize_valid_post(post, parsed))

    assert post.edits[0][0] == "#кр #модбп\n<i>упр</i>\nна 29.10"


def test_seminar_after_lecture_same_day_survives_date_replacement(schedule):
    post = FakePost("#кр #пис\nтест\nна паре")
    parsed, _ = bot.parse_post_detailed(post.text, posted_on=POSTED_AT.date())

    asyncio.run(bot.normalize_valid_post(post, parsed))
    replaced = post.edits[0][0]
    reparsed, error = bot.parse_post_detailed(replaced, posted_on=POSTED_AT.date())

    assert parsed["start"] == "18:55"
    assert error is None
    assert reparsed["start"] == "18:55"


# ── Канал ──────────────────────────────────────────────────
def test_post_from_foreign_channel_is_ignored(schedule, monkeypatch):
    monkeypatch.setattr(config, "HOMEWORK_CHANNEL_ID", CHANNEL_ID, raising=False)
    post = FakePost("#дз #модбп\nупр 1\nдо 29.10", chat_id=-100111)

    asyncio.run(bot.handle_channel_post(SimpleNamespace(channel_post=post), SimpleNamespace()))

    assert saved_homework() == {}
    assert post.reactions == []


def test_post_from_allowed_channel_is_saved_with_chat(schedule, monkeypatch):
    monkeypatch.setattr(config, "HOMEWORK_CHANNEL_ID", CHANNEL_ID, raising=False)
    post = FakePost("#кр #модбп\nна листочках\nна 29.10, 2 пара")

    asyncio.run(bot.handle_channel_post(SimpleNamespace(channel_post=post), SimpleNamespace()))

    record = saved_homework()["10"]
    assert record["kind"] == "control"
    assert record["start"] == "20:30"
    assert record["chat_id"] == CHANNEL_ID
    assert post.reactions == ["👍"]


def test_announcement_mentioning_markers_is_left_alone(schedule):
    post = FakePost("обновление бота\n\nконтрольные теперь через #кр\n#кр #модбп\nна 29.10")

    asyncio.run(bot.handle_channel_post(SimpleNamespace(channel_post=post), SimpleNamespace()))

    assert post.edits == []
    assert post.reactions == []


def test_edited_non_homework_post_loses_bot_feedback_and_reaction(schedule):
    text = ("обновление бота\n\nконтрольные теперь через #кр\n\n"
            "⚠️ Бот: не указан тег предмета в первой строке. Например: #кр #англ.")
    post = FakePost(text)

    asyncio.run(bot.handle_edited_post(SimpleNamespace(edited_channel_post=post), SimpleNamespace()))

    assert post.edits[0][0] == "обновление бота\n\nконтрольные теперь через #кр"
    assert post.reactions == [None]


def test_edit_comment_replies_to_discussion_copy(schedule, monkeypatch):
    monkeypatch.setattr(config, "HOMEWORK_CHANNEL_ID", CHANNEL_ID, raising=False)
    copy = SimpleNamespace(
        is_automatic_forward=True,
        forward_origin=MessageOriginChannel(
            date=POSTED_AT, chat=Chat(id=CHANNEL_ID, type="channel"), message_id=10
        ),
        chat_id=-1009990000,
        message_id=555,
    )
    asyncio.run(bot.remember_discussion_copy(SimpleNamespace(message=copy), SimpleNamespace()))
    original = FakePost("#дз #модбп\nупр 1\nдо 29.10")
    asyncio.run(bot.handle_channel_post(SimpleNamespace(channel_post=original), SimpleNamespace()))

    context = SimpleNamespace(bot=FakeBot())
    edited = FakePost("#дз #модбп\nупр 1\nдо 12.11")
    asyncio.run(bot.handle_edited_post(SimpleNamespace(edited_channel_post=edited), context))

    assert len(context.bot.sent) == 1
    comment = context.bot.sent[0]
    assert comment["chat_id"] == -1009990000
    assert comment["reply_parameters"].message_id == 555


# ── Админка ────────────────────────────────────────────────
def test_schedule_refresh_runs_in_background(paths, monkeypatch):
    release = threading.Event()

    def slow_run(**kwargs):
        release.wait(5)
        return {"lessons": 7, "duration_sec": 1.0}

    monkeypatch.setattr(ruz_schedule, "run", slow_run)
    query = FakeQuery("sched:refresh")

    async def scenario():
        try:
            await asyncio.wait_for(bot.on_callback(admin_update(query), admin_context()), 1)
        finally:
            release.set()
        await bot._refresh_task

    asyncio.run(scenario())

    assert "Обновлено" in query.edits[-1]


def test_stale_button_press_is_ignored(paths):
    stale = BadRequest("Query is too old and response timeout expired or query id is invalid")
    query = FakeQuery("nav:root", answer_error=stale)

    async def scenario():
        await bot.on_callback(admin_update(query), admin_context())

    asyncio.run(scenario())

    assert query.edits, "экран должен отрисоваться даже без ответа на нажатие"


# ── Сторож ─────────────────────────────────────────────────
def test_heartbeat_restarts_process_when_polling_stopped(paths, monkeypatch):
    exits = []
    monkeypatch.setattr(bot, "_exit", exits.append)
    monkeypatch.setattr(bot, "_updater_down", 0)
    application = SimpleNamespace(running=True, updater=SimpleNamespace(running=False))
    context = SimpleNamespace(application=application)

    asyncio.run(bot.heartbeat(context))
    assert exits == []
    asyncio.run(bot.heartbeat(context))
    assert exits == [1]


# ── Хранилище ──────────────────────────────────────────────
def test_corrupted_homework_is_restored_from_backup(paths):
    good = {"10": {"subject": "Сети", "deadline": "2026-10-29"}}
    config.HOMEWORK_JSON.write_text("{broken", encoding="utf-8")
    backup = config.HOMEWORK_JSON.with_name("homework.json.bak")
    backup.write_text(json.dumps(good), encoding="utf-8")

    assert bot.load_homework() == good
    assert bot.load_homework() == good
    assert list(paths.data.glob("homework.json.corrupt-*"))


def test_corrupted_homework_without_backup_starts_empty_and_keeps_copy(paths):
    config.HOMEWORK_JSON.write_text("{broken", encoding="utf-8")

    assert bot.load_homework() == {}
    assert list(paths.data.glob("homework.json.corrupt-*"))


def test_save_keeps_previous_version_as_backup(paths):
    bot.save_homework({"1": {"subject": "А"}})
    bot.save_homework({"2": {"subject": "Б"}})

    backup = config.HOMEWORK_JSON.with_name("homework.json.bak")
    assert json.loads(backup.read_text(encoding="utf-8")) == {"1": {"subject": "А"}}
