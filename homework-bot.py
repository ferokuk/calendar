"""
Telegram-бот для домашки: парсит посты канала, ведёт homework.ics и держит
админку с кнопками в личке.

Формат поста:
    #дз #англ
    упр 14 юнит 6
    до 05.04
"""

import asyncio
import html
import json
import logging
import re
from datetime import datetime, time as dtime, timedelta, timezone
from hashlib import md5
from pathlib import Path

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReactionTypeEmoji,
    Update,
)
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
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

# Одновременно крутить два обновления расписания незачем
_refresh_lock = asyncio.Lock()
_homework_lock = asyncio.Lock()
_last_failed_update_id: int | None = None


# ── Хранилище ──────────────────────────────────────────────
def _load_json(path: Path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def _save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    ruz_schedule.write_atomic(path, json.dumps(data, ensure_ascii=False, indent=2))


def load_homework() -> dict:
    return _load_json(config.HOMEWORK_JSON, {})


def save_homework(data: dict):
    _save_json(config.HOMEWORK_JSON, data)


def load_tags() -> dict:
    return _load_json(config.TAGS_JSON, DEFAULT_TAGS.copy())


def save_tags(data: dict):
    _save_json(config.TAGS_JSON, data)


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


# ── Расписание ─────────────────────────────────────────────
def find_next_lesson_date(subject: str) -> str | None:
    """Находит дату следующей пары по названию предмета."""
    if not config.SCHEDULE_JSON.exists():
        log.warning("Расписание %s ещё не сгенерировано", config.SCHEDULE_JSON.name)
        return None
    schedule = json.loads(config.SCHEDULE_JSON.read_text(encoding="utf-8"))
    today = datetime.now(tz=config.TZ).strftime("%Y-%m-%d")
    dates = sorted(set(
        l["date"] for l in schedule
        if l["discipline"] == subject and l["date"] > today
    ))
    return dates[0] if dates else None


# ── Парсер постов ──────────────────────────────────────────
def parse_post(text: str) -> dict | None:
    """Парсит пост канала. Возвращает dict или None если формат не подходит."""
    # Убираем маркер #дз перед парсингом
    text = re.sub(r"#дз\s*", "", text, flags=re.IGNORECASE)
    lines = [line.strip() for line in text.strip().split("\n") if line.strip()]
    if not lines:
        return None

    tag_match = re.search(r"#(\S+)", lines[0])
    if not tag_match:
        return None
    tag = tag_match.group(1).lower()
    tags = load_tags()
    subject = tags.get(tag)
    if not subject:
        log.warning("Неизвестный тег #%s — задание пропущено", tag)
        return None

    deadline = None
    deadline_line_idx = None
    needs_date_replace = False  # нужно ли заменить "до пары" на дату в посте
    for i, line in enumerate(lines):
        # Точная дата: до 05.04 или до 05.04.2026
        m = re.match(r"до\s+(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?$", line, re.IGNORECASE)
        if m:
            day, month = int(m.group(1)), int(m.group(2))
            year = int(m.group(3)) if m.group(3) else datetime.now(tz=config.TZ).year
            try:
                parsed_date = datetime(year, month, day, tzinfo=config.TZ)
            except ValueError:
                log.warning("Некорректный дедлайн в посте: %s", line)
                return None
            deadline = parsed_date.strftime("%Y-%m-%d")
            deadline_line_idx = i
            break
        # "до пары" / "до следующей пары"
        if re.match(r"до\s+(следующей\s+)?пары$", line, re.IGNORECASE):
            next_date = find_next_lesson_date(subject)
            if next_date:
                deadline = next_date
                deadline_line_idx = i
                needs_date_replace = True
            else:
                log.warning("Для предмета «%s» не нашлось будущих пар в расписании", subject)
            break

    if not deadline:
        log.warning("Дедлайн не найден в посте: %s", lines[0][:60])
        return None

    desc_lines = []
    for i, line in enumerate(lines):
        if i == 0 and tag_match:
            rest = re.sub(r"#\S+\s*", "", line).strip()
            if rest:
                desc_lines.append(rest)
        elif i != deadline_line_idx:
            desc_lines.append(line)
    description = "\n".join(desc_lines)

    return {
        "tag": tag,
        "subject": subject,
        "description": description,
        "deadline": deadline,
        "needs_date_replace": needs_date_replace,
    }


# ── ICS генератор ─────────────────────────────────────────
def generate_ics(homework: dict):
    """Собирает homework.ics. Отсев старья делает prune_homework до вызова."""
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Homework Bot//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:Домашка",
    ]

    for msg_id, hw in homework.items():
        uid = md5(f"hw-{msg_id}".encode()).hexdigest() + "@homework.bot"
        dt = hw["deadline"].replace("-", "")
        end = datetime.strptime(hw["deadline"], "%Y-%m-%d") + timedelta(days=1)

        lines.extend([
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{datetime.now(tz=timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
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
        ])

    lines.append("END:VCALENDAR")

    config.HOMEWORK_ICS.parent.mkdir(parents=True, exist_ok=True)
    ruz_schedule.write_atomic(config.HOMEWORK_ICS, "\r\n".join(lines))
    log.info("homework.ics обновлён: активных заданий — %d", len(homework))


# ── Реакции ────────────────────────────────────────────────
async def react(post, emoji: str):
    """Ставит реакцию на пост канала."""
    try:
        await post.set_reaction([ReactionTypeEmoji(emoji)])
    except Exception as e:  # noqa: BLE001 — реакция необязательна, ронять обработку нельзя
        log.warning("Не удалось поставить реакцию %s: %s", repr(emoji), notify.describe_error(e))


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
    return sorted(active, key=lambda item: item[1].get("deadline", ""))


def count_active(homework: dict) -> int:
    today = datetime.now(tz=config.TZ).strftime("%Y-%m-%d")
    return sum(hw.get("deadline", "") >= today for hw in homework.values())


# ── Экраны админки ─────────────────────────────────────────
def screen_root() -> tuple[str, InlineKeyboardMarkup]:
    sched = status.read("schedule")
    homework_count = len(active_homework())

    if sched.get("last_error"):
        sched_line = f"🔴 сбой: {esc(sched['last_error'])[:120]}"
    elif sched.get("last_success"):
        sched_line = f"✅ {esc(status.human_time(sched.get('last_success')))}"
    else:
        sched_line = "— ещё не запускалось"

    text = (
        "<b>⚙️ Админка</b>\n\n"
        f"🗓 Расписание: {sched_line}\n"
        f"📚 Домашка: активных заданий — {homework_count}"
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
            blocks.append(
                f"• <b>{esc(hw['subject'])}</b> — {esc(fmt_deadline(hw['deadline']))}\n"
                f"  {esc(desc) if desc else '<i>без описания</i>'}"
            )
        body = "\n".join(blocks)
        if len(items) > 20:
            body += f"\n\n<i>И ещё заданий: {len(items) - 20}</i>"
    else:
        body = "<i>Активных заданий нет.</i>"

    rows = []
    for msg_id, hw in items[:20]:
        label = f"❌ {hw['subject'][:20]} · {hw['deadline'][8:10]}.{hw['deadline'][5:7]}"
        rows.append([InlineKeyboardButton(label, callback_data=f"hw:del:{msg_id}")])
    rows.append([InlineKeyboardButton("🧹 Очистить просроченные", callback_data="hw:prune")])
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data="nav:root")])

    text = (
        f"<b>📚 Домашка — {len(items)} шт.</b>\n\n{body}\n\n"
        f"<i>Хранение: {config.HOMEWORK_RETENTION_DAYS} дн. после дедлайна.</i>"
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
        f"Глубина: {config.RUZ_DAYS_AHEAD} дн. вперёд",
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

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Обновить сейчас", callback_data="sched:refresh")],
        [InlineKeyboardButton("◀️ Назад", callback_data="nav:root")],
    ])
    return "\n".join(lines), keyboard


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

    lines = [
        "<b>📊 Статус</b>\n",
        "<b>Расписание</b>",
        f"  Успех: {esc(status.human_time(sched.get('last_success')))}",
        f"  Длительность: {sched.get('duration_sec', '—')} с",
        f"  Ошибка: {esc(sched.get('last_error') or 'нет')}",
        "",
        "<b>Бот</b>",
        f"  Запущен: {esc(status.human_time(bot.get('started_at')))}",
        f"  Активных ДЗ: {bot.get('homework_active', count_active(load_homework()))}",
        f"  Последнее задание: {esc(status.human_time(bot.get('last_homework_at')))}",
        f"  Последняя чистка: {esc(status.human_time(bot.get('last_cleanup')))}",
        f"  Ошибка: {esc(bot.get('last_error') or 'нет')}",
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


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(update):
        await query.answer("Недоступно", show_alert=True)
        return

    data = query.data or ""

    if data.startswith("nav:"):
        await query.answer()
        await show(update, data.split(":", 1)[1])
        return

    if data == "tag:add":
        context.user_data["awaiting"] = "tag"
        await query.answer()
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
            await query.answer("Тег уже удалён", show_alert=True)
        else:
            subject = tags.pop(target)
            save_tags(tags)
            log.info("Удалён тег #%s (%s)", target, subject)
            await query.answer(f"Удалён #{target}")
        await show(update, "tags")
        return

    if data.startswith("hw:del:"):
        msg_id = data.split(":", 2)[2]
        async with _homework_lock:
            homework = load_homework()
            removed = homework.pop(msg_id, None)
            if not removed:
                await query.answer("Задание уже удалено", show_alert=True)
            else:
                if persist_homework(homework, "удаление задания из админки"):
                    log.info("Удалено задание %s (%s)", msg_id, removed.get("subject"))
                    await query.answer(f"Удалено: {removed.get('subject', '')}"[:200])
                else:
                    await query.answer("Не удалось сохранить", show_alert=True)
        await show(update, "hw")
        return

    if data == "hw:prune":
        async with _homework_lock:
            homework = load_homework()
            before = len(homework)
            prune_homework(homework, retention_days=0)
            if persist_homework(homework, "ручная очистка просроченного"):
                await query.answer(f"Удалено: {before - len(homework)}")
            else:
                await query.answer("Не удалось сохранить", show_alert=True)
        await show(update, "hw")
        return

    if data == "sched:refresh":
        if _refresh_lock.locked():
            await query.answer("Обновление уже идёт", show_alert=True)
            return
        await query.answer("Запустил, это займёт до минуты")
        async with _refresh_lock:
            try:
                await query.edit_message_text("🔄 Обновляю расписание…", parse_mode="HTML")
            except BadRequest:
                pass
            try:
                summary = await asyncio.to_thread(ruz_schedule.run)
                notice = (f"✅ Обновлено: занятий {summary['lessons']} "
                          f"за {summary['duration_sec']} с")
            except Exception as e:  # noqa: BLE001 — текст ошибки нужен на экране
                notice = f"🔴 Не удалось: {esc(notify.describe_error(e))}"
            await show(update, "sched", notice=notice)
        return

    await query.answer()


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
async def handle_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    post = update.channel_post
    if not post or not post.text:
        return

    # Парсим только посты с #дз
    if "#дз" not in post.text.lower():
        return

    parsed = parse_post(post.text)
    if not parsed:
        await react(post, "👎")
        return

    msg_id = str(post.message_id)
    async with _homework_lock:
        homework = load_homework()
        homework[msg_id] = parsed
        if not persist_homework(homework, f"новое задание #{parsed['tag']}"):
            await react(post, "👎")
            return

    # Если "до пары" — заменяем на точную дату в посте
    if parsed.get("needs_date_replace"):
        dt = datetime.strptime(parsed["deadline"], "%Y-%m-%d")
        new_text = re.sub(
            r"до\s+(следующей\s+)?пары",
            f"до {dt.strftime('%d.%m')}",
            post.text,
            flags=re.IGNORECASE,
        )
        try:
            await post.edit_text(new_text)
        except Exception as e:  # noqa: BLE001 — правка поста необязательна
            log.warning("Не удалось отредактировать пост %s: %s",
                        msg_id, notify.describe_error(e))

    await react(post, "👍")
    log.info("Новое задание #%s (%s) до %s: %s",
             parsed["tag"], parsed["subject"], parsed["deadline"],
             parsed["description"][:50])


async def handle_edited_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    post = update.edited_channel_post
    if not post or not post.text:
        return

    msg_id = str(post.message_id)
    if "#дз" not in post.text.lower():
        async with _homework_lock:
            homework = load_homework()
            old = homework.get(msg_id)
            if old:
                del homework[msg_id]
                if not persist_homework(homework, f"снятие #дз с поста {msg_id}"):
                    await react(post, "👎")
                    return
                await react(post, "👌")
                log.info("Задание %s удалено — из поста убран #дз", msg_id)
        return

    parsed = parse_post(post.text)
    if not parsed:
        await react(post, "👎")
        return

    async with _homework_lock:
        homework = load_homework()
        old = homework.get(msg_id)
        # Это фильтрует собственную правку «до пары» → дата.
        if old and old.get("deadline") == parsed["deadline"] \
               and old.get("description") == parsed["description"] \
               and old.get("tag") == parsed["tag"]:
            log.debug("Задание %s: данные не изменились, пропускаем", msg_id)
            return

        homework[msg_id] = parsed
        if not persist_homework(homework, f"правка задания {msg_id}"):
            await react(post, "👎")
            return

    await react(post, "✍️")

    changes = []
    if old and old.get("deadline") != parsed["deadline"]:
        changes.append(f"дедлайн: {old['deadline']} → {parsed['deadline']}")
    if old and old.get("description") != parsed["description"]:
        changes.append("описание обновлено")
    if old and old.get("tag") != parsed["tag"]:
        changes.append(f"предмет: #{old['tag']} → #{parsed['tag']}")

    if changes:
        now = datetime.now(tz=config.TZ).strftime("%d.%m %H:%M")
        comment = f"✏️ Изменения учтены ({now})\n" + "\n".join(f"• {c}" for c in changes)
        try:
            chat = await context.bot.get_chat(post.chat_id)
            if chat.linked_chat_id:
                await context.bot.send_message(
                    chat_id=chat.linked_chat_id,
                    text=comment,
                    reply_to_message_id=post.message_id,
                )
        except Exception as e:  # noqa: BLE001 — комментарий необязателен
            log.info("Комментарий к посту %s не отправлен: %s",
                     msg_id, notify.describe_error(e))

    log.info("Изменено задание #%s (%s) до %s", parsed["tag"], parsed["subject"],
             parsed["deadline"])


# ── Фон и ошибки ───────────────────────────────────────────
async def daily_cleanup(context: ContextTypes.DEFAULT_TYPE):
    """
    Ежедневная чистка. Нужна отдельно от обработчиков: без неё просроченное
    задание висело бы в календаре до следующего поста в канале.
    """
    async with _homework_lock:
        homework = load_homework()
        removed = prune_homework(homework)
        if not persist_homework(homework, "ежедневный ретеншен", record_activity=False):
            return

    status.update("bot", last_cleanup=status.now_iso(), homework_active=count_active(homework))
    log.info("Ежедневная чистка: удалено %d, осталось %d", removed, len(homework))


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Любая необработанная ошибка обработчика — в лог и в уведомление."""
    global _last_failed_update_id
    error = context.error
    _last_failed_update_id = update.update_id if isinstance(update, Update) else None
    exc_info = ((type(error), error, error.__traceback__)
                if isinstance(error, BaseException) else None)
    log.error("Необработанная ошибка обработчика", exc_info=exc_info)
    status.update(
        "bot",
        last_error=notify.describe_error(error),
        last_error_at=status.now_iso(),
        last_error_source="runtime",
    )

    where = "обработка обновления"
    if isinstance(update, Update) and update.effective_message:
        where = f"сообщение {update.effective_message.message_id}"
    await asyncio.to_thread(
        notify.alert,
        "Ошибка в боте",
        error or "неизвестная ошибка",
        step=where,
        key=ALERT_KEY_RUNTIME,
    )


async def mark_runtime_healthy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Следующий успешно обработанный update закрывает runtime-алерт."""
    global _last_failed_update_id
    runtime_status = status.read("bot").get("last_error_source") == "runtime"
    if not runtime_status and not notify.is_failing(ALERT_KEY_RUNTIME):
        return
    if update.update_id == _last_failed_update_id:
        return
    recovered = await asyncio.to_thread(
        notify.recovered, ALERT_KEY_RUNTIME, "Бот снова обрабатывает сообщения"
    )
    if runtime_status and (recovered or not config.ALERTS_ENABLED):
        _last_failed_update_id = None
        status.update(
            "bot", last_error=None, last_error_at=None, last_error_source=None
        )


async def on_startup(app: Application):
    status.update(
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
    if not persist_homework(homework, "инициализация хранилища", record_activity=False):
        raise RuntimeError("не удалось инициализировать хранилище домашки")
    await asyncio.to_thread(notify.recovered, ALERT_KEY_RUNTIME, "Бот снова работает")
    log.info("Бот запущен. Активных заданий: %d, всего хранится: %d, админ: %s",
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
    app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POST, handle_channel_post))
    app.add_handler(MessageHandler(filters.UpdateType.EDITED_CHANNEL_POST, handle_edited_post))
    app.add_handler(TypeHandler(Update, mark_runtime_healthy), group=1)

    app.add_error_handler(on_error)

    if app.job_queue:
        app.job_queue.run_daily(
            daily_cleanup,
            time=dtime(hour=config.HOMEWORK_CLEANUP_HOUR, minute=0, tzinfo=config.TZ),
            name="homework-retention",
        )
        log.info("Чистка домашки запланирована на %02d:00 каждый день",
                 config.HOMEWORK_CLEANUP_HOUR)
    else:
        log.warning("JobQueue недоступна — ежедневная чистка не запланирована. "
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
