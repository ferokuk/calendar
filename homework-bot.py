"""
Telegram-бот для парсинга домашних заданий из канала и генерации .ics файла.

Формат поста:
    #англ
    упр 14 юнит 6
    до 05.04
"""

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from hashlib import md5
from pathlib import Path

from telegram import ReactionTypeEmoji, Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

# ── Настройки ──────────────────────────────────────────────
BOT_TOKEN = os.environ["TG_BOT_TOKEN"]
HOMEWORK_JSON = Path("/app/data/homework.json")
TAGS_JSON = Path("/app/data/tags.json")
SCHEDULE_JSON = Path("/app/calendars/schedule.json")
OUTPUT_ICS = Path("/app/calendars/homework.ics")
CLEANUP_DAYS = 14  # удалять дедлайны старше N дней
ADMIN_USERNAME = "ferokuk"

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

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)


# ── Хранилище ──────────────────────────────────────────────
def _load_json(path: Path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def _save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_homework() -> dict:
    return _load_json(HOMEWORK_JSON, {})


def save_homework(data: dict):
    _save_json(HOMEWORK_JSON, data)


def load_tags() -> dict:
    return _load_json(TAGS_JSON, DEFAULT_TAGS.copy())


def save_tags(data: dict):
    _save_json(TAGS_JSON, data)


# ── Расписание ─────────────────────────────────────────────
def find_next_lesson_date(subject: str) -> str | None:
    """Находит дату следующей пары по названию предмета."""
    if not SCHEDULE_JSON.exists():
        return None
    schedule = json.loads(SCHEDULE_JSON.read_text(encoding="utf-8"))
    today = datetime.now().strftime("%Y-%m-%d")
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
        log.warning(f"Неизвестный тег: #{tag}")
        return None

    deadline = None
    deadline_line_idx = None
    needs_date_replace = False  # нужно ли заменить "до пары" на дату в посте
    for i, line in enumerate(lines):
        # Точная дата: до 05.04 или до 05.04.2026
        m = re.match(r"до\s+(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?$", line, re.IGNORECASE)
        if m:
            day, month = int(m.group(1)), int(m.group(2))
            year = int(m.group(3)) if m.group(3) else datetime.now().year
            deadline = f"{year:04d}-{month:02d}-{day:02d}"
            deadline_line_idx = i
            break
        # "до пары" / "до следующей пары"
        if re.match(r"до\s+(следующей\s+)?пары$", line, re.IGNORECASE):
            next_date = find_next_lesson_date(subject)
            if next_date:
                deadline = next_date
                deadline_line_idx = i
                needs_date_replace = True
            break

    if not deadline:
        log.warning("Дедлайн не найден в посте")
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
def escape_ics(text: str) -> str:
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def generate_ics(homework: dict):
    cutoff = (datetime.now() - timedelta(days=CLEANUP_DAYS)).strftime("%Y-%m-%d")

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Homework Bot//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:Домашка",
    ]

    count = 0
    for msg_id, hw in homework.items():
        if hw["deadline"] < cutoff:
            continue
        count += 1

        uid = md5(f"hw-{msg_id}".encode()).hexdigest() + "@homework.bot"
        dt = hw["deadline"].replace("-", "")
        end = datetime.strptime(hw["deadline"], "%Y-%m-%d") + timedelta(days=1)
        dt_end = end.strftime("%Y%m%d")

        lines.extend([
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{datetime.now(tz=timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
            f"DTSTART;VALUE=DATE:{dt}",
            f"DTEND;VALUE=DATE:{dt_end}",
            f"SUMMARY:ДЗ: {escape_ics(hw['subject'])}",
            f"DESCRIPTION:{escape_ics(hw['description'])}",
            "BEGIN:VALARM",
            "TRIGGER:-P1D",
            "ACTION:DISPLAY",
            "DESCRIPTION:Дедлайн завтра",
            "END:VALARM",
            "END:VEVENT",
        ])

    lines.append("END:VCALENDAR")

    OUTPUT_ICS.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_ICS.write_text("\r\n".join(lines), encoding="utf-8")
    log.info(f"ICS обновлён: {count} активных заданий")


# ── Реакции ────────────────────────────────────────────────
async def react(post, emoji: str):
    """Ставит реакцию на пост канала."""
    try:
        await post.set_reaction([ReactionTypeEmoji(emoji)])
    except Exception as e:
        log.warning(f"Не удалось поставить реакцию {repr(emoji)}: {e}")


# ── Админские команды (только @ferokuk в ЛС бота) ─────────
def is_admin(update: Update) -> bool:
    user = update.effective_user
    return user and user.username and user.username.lower() == ADMIN_USERNAME


async def cmd_tags(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать все теги: /tags"""
    if not is_admin(update):
        return
    tags = load_tags()
    lines = [f"#{k} — {v}" for k, v in sorted(tags.items())]
    await update.message.reply_text("Текущие теги:\n" + "\n".join(lines))


async def cmd_addtag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/addtag тег Название предмета"""
    if not is_admin(update):
        return
    if not context.args or len(context.args) < 2:
        await update.message.reply_text("Формат: /addtag тег Название предмета\nПример: /addtag линал Линейная алгебра")
        return
    tag = context.args[0].lower().lstrip("#")
    subject = " ".join(context.args[1:])
    tags = load_tags()
    tags[tag] = subject
    save_tags(tags)
    await update.message.reply_text(f"Добавлен: #{tag} — {subject}")


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
    await update.message.reply_text(f"Удалён: #{tag} — {subject}")


# ── Обработчики ────────────────────────────────────────────
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
    homework = load_homework()
    homework[msg_id] = parsed
    save_homework(homework)
    generate_ics(homework)

    # Если "до пары" — заменяем на точную дату в посте
    if parsed.get("needs_date_replace"):
        dt = datetime.strptime(parsed["deadline"], "%Y-%m-%d")
        date_str = dt.strftime("%d.%m")
        new_text = re.sub(
            r"до\s+(следующей\s+)?пары",
            f"до {date_str}",
            post.text,
            flags=re.IGNORECASE,
        )
        try:
            await post.edit_text(new_text)
        except Exception as e:
            log.warning(f"Не удалось отредактировать пост: {e}")

    await react(post, "👍")
    log.info(f"Новое задание #{parsed['tag']}: {parsed['description'][:50]}... до {parsed['deadline']}")


async def handle_edited_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    post = update.edited_channel_post
    if not post or not post.text:
        return

    msg_id = str(post.message_id)
    homework = load_homework()
    old = homework.get(msg_id)

    # Если #дз убрали из поста — удаляем задание из календаря
    if "#дз" not in post.text.lower():
        if old:
            del homework[msg_id]
            save_homework(homework)
            generate_ics(homework)
            await react(post, "👌")
            log.info(f"Задание {msg_id} удалено (убран #дз)")
        return

    parsed = parse_post(post.text)
    if not parsed:
        await react(post, "👎")
        return

    # Сравниваем данные — если ничего не изменилось, игнорируем
    # (это фильтрует собственные правки бота при замене "до пары" → дату)
    if old and old.get("deadline") == parsed["deadline"] \
           and old.get("description") == parsed["description"] \
           and old.get("tag") == parsed["tag"]:
        log.info(f"Задание {msg_id}: данные не изменились, пропускаем")
        return

    homework[msg_id] = parsed
    save_homework(homework)
    generate_ics(homework)

    await react(post, "\u270d\ufe0f")

    # Комментарий об изменениях
    now = datetime.now(tz=timezone(timedelta(hours=3))).strftime("%d.%m %H:%M")
    changes = []
    if old and old.get("deadline") != parsed["deadline"]:
        changes.append(f"дедлайн: {old['deadline']} → {parsed['deadline']}")
    if old and old.get("description") != parsed["description"]:
        changes.append("описание обновлено")
    if old and old.get("tag") != parsed["tag"]:
        changes.append(f"предмет: #{old['tag']} → #{parsed['tag']}")

    if changes:
        comment = f"✏️ Изменения учтены ({now})\n" + "\n".join(f"• {c}" for c in changes)
        try:
            chat = await context.bot.get_chat(post.chat_id)
            if chat.linked_chat_id:
                await context.bot.send_message(
                    chat_id=chat.linked_chat_id,
                    text=comment,
                    reply_to_message_id=post.message_id,
                )
        except Exception as e:
            log.info(f"Комментарий не отправлен: {e}")

    log.info(f"Изменено задание #{parsed['tag']}: до {parsed['deadline']}")



# ── Запуск ─────────────────────────────────────────────────
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # Админские команды (в ЛС боту)
    app.add_handler(CommandHandler("tags", cmd_tags))
    app.add_handler(CommandHandler("addtag", cmd_addtag))
    app.add_handler(CommandHandler("deltag", cmd_deltag))

    # Канальные посты
    app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POST, handle_channel_post))
    app.add_handler(MessageHandler(filters.UpdateType.EDITED_CHANNEL_POST, handle_edited_post))

    log.info("Бот запущен")
    app.run_polling(allowed_updates=["channel_post", "edited_channel_post", "message"])


if __name__ == "__main__":
    main()
