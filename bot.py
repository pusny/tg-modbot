import subprocess
import sys


def _ensure(pkg_spec: str, import_name: str) -> None:
    try:
        __import__(import_name)
    except ImportError:
        subprocess.check_call([sys.executable, "-m", "pip", "install", pkg_spec])


_ensure("python-telegram-bot>=21,<22", "telegram")
_ensure("python-dotenv", "dotenv")


import asyncio
import html
import logging
import os
import re
import sqlite3
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv()


def _req(key: str) -> str:
    val = os.getenv(key)
    if val is None or val.strip() == "":
        raise RuntimeError(
            f"Не задана переменная: {key}. Проверь .env ({Path.cwd()})"
        )
    return val.strip()


def _req_int(key: str, default: Optional[int] = None) -> int:
    val = os.getenv(key)
    if val is None or val.strip() == "":
        if default is None:
            raise RuntimeError(f"Не задана переменная: {key}")
        return default
    return int(val.strip())


BOT_TOKEN        = _req("BOT_TOKEN")
ADMIN_CHAT_ID    = _req_int("ADMIN_CHAT_ID")
REPORT_THREAD_ID = _req_int("REPORT_THREAD_ID", 0) or None
DB_PATH          = os.getenv("DB_PATH", "bot.db")

DEFAULT_EPHEMERAL_DELAY = 60.0
MIN_TG_MUTE_SEC = 60

CONNECT_TIMEOUT   = 30.0
READ_TIMEOUT      = 30.0
WRITE_TIMEOUT     = 30.0
POOL_TIMEOUT      = 30.0
START_RETRY_DELAY = 10.0


from telegram import (
    BotCommand,
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import NetworkError, TimedOut
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


_fmt = logging.Formatter(
    "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    "%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("camelot_mod")
log.setLevel(logging.INFO)
if not log.handlers:
    _c = logging.StreamHandler()
    _c.setFormatter(_fmt)
    log.addHandler(_c)

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

log.info("Config loaded | cwd=%s | admin=%s | thread=%s",
         Path.cwd(), ADMIN_CHAT_ID, REPORT_THREAD_ID)


RANK_OWNER        = 100
RANK_SENIOR_ADMIN = 80
RANK_JUNIOR_ADMIN = 60
RANK_SENIOR_MOD   = 40
RANK_JUNIOR_MOD   = 20
RANK_USER         = 0

RANK_NAMES = {
    RANK_OWNER:        "👑 Создатель",
    RANK_SENIOR_ADMIN: "🛡 Старший администратор",
    RANK_JUNIOR_ADMIN: "🛡 Младший администратор",
    RANK_SENIOR_MOD:   "⚔ Старший модератор",
    RANK_JUNIOR_MOD:   "⚔ Младший модератор",
    RANK_USER:         "👤 Участник",
}

RANK_ALIASES = {
    "owner":           RANK_OWNER,
    "владелец":        RANK_OWNER,
    "создатель":       RANK_OWNER,
    "senior_admin":    RANK_SENIOR_ADMIN,
    "старший_админ":   RANK_SENIOR_ADMIN,
    "ст_админ":        RANK_SENIOR_ADMIN,
    "junior_admin":    RANK_JUNIOR_ADMIN,
    "младший_админ":   RANK_JUNIOR_ADMIN,
    "мл_админ":        RANK_JUNIOR_ADMIN,
    "senior_mod":      RANK_SENIOR_MOD,
    "старший_мод":     RANK_SENIOR_MOD,
    "ст_мод":          RANK_SENIOR_MOD,
    "junior_mod":      RANK_JUNIOR_MOD,
    "младший_мод":     RANK_JUNIOR_MOD,
    "мл_мод":          RANK_JUNIOR_MOD,
    "user":            RANK_USER,
    "участник":        RANK_USER,
    "юзер":            RANK_USER,
}

RANK_HINTS = (
    "owner / владелец / создатель\n"
    "senior_admin / старший_админ / ст_админ\n"
    "junior_admin / младший_админ / мл_админ\n"
    "senior_mod / старший_мод / ст_мод\n"
    "junior_mod / младший_мод / мл_мод\n"
    "user / участник / юзер"
)

ACTION_ALIASES = {
    "warn": "warn", "варн": "warn", "предупреждение": "warn", "пред": "warn",
    "mute": "mute", "мут": "mute",
    "kick": "kick", "кик": "kick",
    "ban": "ban", "бан": "ban",
}

ACTION_HINTS = "варн / мут / кик / бан"

MUTE_PERMS = ChatPermissions(can_send_messages=False)
UNMUTE_PERMS = ChatPermissions(
    can_send_messages=True,
    can_send_audios=True,
    can_send_documents=True,
    can_send_photos=True,
    can_send_videos=True,
    can_send_video_notes=True,
    can_send_voice_notes=True,
    can_send_polls=True,
    can_send_other_messages=True,
    can_add_web_page_previews=True,
)


def esc(value) -> str:
    if value is None:
        return ""
    return html.escape(str(value), quote=False)


def _msg_link(chat_id: int, message_id: Optional[int]) -> str:
    if not message_id:
        return ""
    s = str(chat_id)
    if s.startswith("-100"):
        return f"https://t.me/c/{s[4:]}/{message_id}"
    return ""


_pending_tasks: set = set()


def _schedule(coro):
    try:
        task = asyncio.create_task(coro)
        _pending_tasks.add(task)
        task.add_done_callback(_pending_tasks.discard)
    except Exception as e:
        log.debug("_schedule fail: %s", e)


async def safe(coro_fn, *args, retries: int = 3, **kwargs):
    for attempt in range(1, retries + 1):
        try:
            return await coro_fn(*args, **kwargs)
        except (TimedOut, NetworkError) as e:
            if attempt == retries:
                log.warning("safe(): окончательно упало — %s", e)
                return None
            await asyncio.sleep(1.5 * attempt)
        except Exception as e:
            log.debug("safe(): non-network error — %s", e)
            return None


async def _del_later(bot, chat_id: int, message_id: int, delay: float):
    await asyncio.sleep(delay)
    await safe(bot.delete_message, chat_id, message_id)


async def _delayed(bot, chat_id: int, message_id: int):
    """Планирует автоудаление сообщения через ephemeral_delay этого чата."""
    try:
        s = await get_settings(chat_id)
        delay = int(_s(s, "ephemeral_delay", int(DEFAULT_EPHEMERAL_DELAY)))
    except Exception:
        delay = int(DEFAULT_EPHEMERAL_DELAY)
    if delay > 0:
        _schedule(_del_later(bot, chat_id, message_id, float(delay)))


async def eph(msg, text: str, delay: Optional[float] = None, bot=None, **kwargs):
    sent = await safe(msg.reply_text, text, **kwargs)
    if sent is None:
        return sent
    if delay is None:
        await _delayed(bot or msg.bot if False else (bot or msg.chat.id and bot), msg.chat.id, sent.message_id) if False else None
    # упрощаем: если bot не передан — попробуем вытащить через get_bot
    actual_bot = bot
    if actual_bot is None:
        try:
            gb = getattr(msg, "get_bot", None)
            if gb is not None:
                maybe = gb()
                if asyncio.iscoroutine(maybe):
                    actual_bot = await maybe
                else:
                    actual_bot = maybe
        except Exception:
            actual_bot = None
    if actual_bot is None:
        return sent
    if delay is None:
        await _delayed(actual_bot, msg.chat.id, sent.message_id)
    elif delay > 0:
        _schedule(_del_later(actual_bot, msg.chat.id, sent.message_id, float(delay)))
    return sent


async def send_admin(bot, text: str, **kwargs):
    kw = dict(kwargs)
    if REPORT_THREAD_ID:
        kw["message_thread_id"] = REPORT_THREAD_ID
    sent = await safe(bot.send_message, ADMIN_CHAT_ID, text, **kw)
    if sent is None and REPORT_THREAD_ID:
        kw.pop("message_thread_id", None)
        sent = await safe(bot.send_message, ADMIN_CHAT_ID, text, **kw)
    if sent is None:
        log.warning("send_admin: не удалось доставить лог в %s", ADMIN_CHAT_ID)
    return sent


async def log_action(bot, actor, action: str, target, reason: str = "", chat_id: int = 0,
                     reply_msg_id: Optional[int] = None):
    if actor is None:
        actor_line = "🤖 <i>Автоматика</i>"
    else:
        actor_line = actor.mention_html()
        actor_line += f" (<code>{actor.id}</code>)"

    if hasattr(target, "mention_html"):
        target_line = f"{target.mention_html()} (<code>{target.id}</code>)"
    else:
        target_line = f"<code>{target}</code>"

    text = (
        f"📋 <b>Действие модератора</b>\n\n"
        f"👮 <b>Кто:</b> {actor_line}\n"
        f"🎯 <b>Кого:</b> {target_line}\n"
        f"⚙️ <b>Что:</b> {action}\n"
        f"💬 <b>Чат:</b> <code>{chat_id}</code>"
    )
    link = _msg_link(chat_id, reply_msg_id)
    if link:
        text += f"\n<a href='{link}'>→ Сообщение</a>"
    if reason:
        text += f"\n\n📝 <b>Причина:</b> {esc(reason)}"
    await send_admin(bot, text, parse_mode=ParseMode.HTML,
                     disable_web_page_preview=True)


def parse_duration(s: str) -> int:
    if not s:
        return 0
    s = s.strip().lower()
    unit = s[-1]
    try:
        n = int(s[:-1])
    except ValueError:
        return 0
    multipliers = {
        "s": 1, "с": 1,
        "m": 60, "м": 60,
        "h": 3600, "ч": 3600,
        "d": 86400, "д": 86400,
    }
    return n * multipliers.get(unit, 0)


def parse_setting_time(s: str) -> int:
    s = s.strip().lower()
    if not s:
        return 0
    if s[-1].isdigit():
        try:
            return int(s)
        except ValueError:
            return 0
    return parse_duration(s)


def fmt_seconds(sec: int) -> str:
    if sec >= 86400: return f"{sec // 86400}д"
    if sec >= 3600:  return f"{sec // 3600}ч"
    if sec >= 60:    return f"{sec // 60}м"
    return f"{sec}с"


LINK_RE = re.compile(r"(https?://|t\.me/|telegram\.me/|@[A-Za-z0-9_]{5,})", re.IGNORECASE)


SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS users (
    user_id     INTEGER,
    chat_id     INTEGER,
    username    TEXT,
    first_name  TEXT,
    rank        INTEGER NOT NULL DEFAULT 0,
    warns       INTEGER NOT NULL DEFAULT 0,
    mute_until  TEXT,
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, chat_id)
);

CREATE TABLE IF NOT EXISTS triggers (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id  INTEGER NOT NULL,
    word     TEXT NOT NULL,
    action   TEXT NOT NULL DEFAULT 'warn'
);

CREATE TABLE IF NOT EXISTS circles (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    INTEGER NOT NULL,
    name       TEXT NOT NULL,
    owner_id   INTEGER NOT NULL,
    thread_id  INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS circle_members (
    circle_id  INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    PRIMARY KEY (circle_id, user_id)
);

CREATE TABLE IF NOT EXISTS topics (
    chat_id        INTEGER NOT NULL,
    thread_id      INTEGER NOT NULL,
    links_allowed  INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (chat_id, thread_id)
);

CREATE TABLE IF NOT EXISTS settings (
    chat_id            INTEGER PRIMARY KEY,
    antispam           INTEGER NOT NULL DEFAULT 1,
    antiraid           INTEGER NOT NULL DEFAULT 1,
    triggers_on        INTEGER NOT NULL DEFAULT 1,
    flood_limit        INTEGER NOT NULL DEFAULT 7,
    flood_window       INTEGER NOT NULL DEFAULT 10,
    mute_minutes       INTEGER NOT NULL DEFAULT 30,
    warn_limit         INTEGER NOT NULL DEFAULT 3,
    auto_mute_min      INTEGER NOT NULL DEFAULT 60,
    trig_mute_min      INTEGER NOT NULL DEFAULT 60,
    default_mute_min   INTEGER NOT NULL DEFAULT 10
);

CREATE TABLE IF NOT EXISTS reports (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id      INTEGER NOT NULL,
    thread_id    INTEGER DEFAULT 0,
    reporter_id  INTEGER NOT NULL,
    target_id    INTEGER NOT NULL,
    reason       TEXT,
    msg_link     TEXT,
    status       TEXT NOT NULL DEFAULT 'open',
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS messages (
    chat_id     INTEGER NOT NULL,
    message_id  INTEGER NOT NULL,
    thread_id   INTEGER NOT NULL DEFAULT 0,
    user_id     INTEGER,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (chat_id, message_id)
);

CREATE INDEX IF NOT EXISTS idx_messages_thread
    ON messages (chat_id, thread_id, message_id DESC);

CREATE TABLE IF NOT EXISTS blacklist (
    user_id     INTEGER,
    chat_id     INTEGER,
    reason      TEXT,
    blocked_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, chat_id)
);
"""

SETTINGS_MIGRATIONS = [
    ("triggers_on",       "INTEGER NOT NULL DEFAULT 1"),
    ("warn_limit",        "INTEGER NOT NULL DEFAULT 3"),
    ("auto_mute_sec",     "INTEGER NOT NULL DEFAULT 3600"),
    ("trig_mute_sec",     "INTEGER NOT NULL DEFAULT 3600"),
    ("default_mute_sec",  "INTEGER NOT NULL DEFAULT 600"),
    ("flood_mute_sec",    "INTEGER NOT NULL DEFAULT 1800"),
    ("warn_action",       "TEXT NOT NULL DEFAULT 'ban'"),
    ("antiraid_joins",    "INTEGER NOT NULL DEFAULT 10"),
    ("antiraid_window",   "INTEGER NOT NULL DEFAULT 30"),
    ("antiraid_lookback", "INTEGER NOT NULL DEFAULT 1800"),
    ("ephemeral_delay",   "INTEGER NOT NULL DEFAULT 60"),
    ("trig_warn_limit",   "INTEGER NOT NULL DEFAULT 3"),
    ("links_forbidden",   "INTEGER NOT NULL DEFAULT 0"),
]


def _sync_init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript(SCHEMA)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(settings)")}
        for col, ddl in SETTINGS_MIGRATIONS:
            if col not in cols:
                conn.execute(f"ALTER TABLE settings ADD COLUMN {col} {ddl}")
        conn.commit()


def _sync_exec(query: str, params: tuple = (), fetch: Optional[str] = None):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(query, params)
        if fetch == "one":
            return cur.fetchone()
        if fetch == "all":
            return cur.fetchall()
        conn.commit()
        return cur.lastrowid


async def db_exec(query: str, params: tuple = (), fetch: Optional[str] = None):
    return await asyncio.to_thread(_sync_exec, query, params, fetch)


async def upsert_user(user_id: int, chat_id: int, username: Optional[str], first_name: Optional[str]):
    await db_exec(
        """
        INSERT INTO users (user_id, chat_id, username, first_name)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id, chat_id) DO UPDATE SET
            username = excluded.username,
            first_name = excluded.first_name,
            updated_at = CURRENT_TIMESTAMP
        """,
        (user_id, chat_id, username, first_name),
    )


async def get_user(user_id: int, chat_id: int):
    return await db_exec(
        "SELECT * FROM users WHERE user_id=? AND chat_id=?",
        (user_id, chat_id), fetch="one",
    )


async def set_rank(user_id: int, chat_id: int, rank: int):
    await upsert_user(user_id, chat_id, None, None)
    await db_exec(
        "UPDATE users SET rank=? WHERE user_id=? AND chat_id=?",
        (rank, user_id, chat_id),
    )


async def get_rank(user_id: int, chat_id: int) -> int:
    row = await db_exec(
        "SELECT rank FROM users WHERE user_id=? AND chat_id=?",
        (user_id, chat_id), fetch="one",
    )
    return row["rank"] if row else 0


async def add_warn(user_id: int, chat_id: int, delta: int = 1) -> int:
    await upsert_user(user_id, chat_id, None, None)
    await db_exec(
        "UPDATE users SET warns = warns + ? WHERE user_id=? AND chat_id=?",
        (delta, user_id, chat_id),
    )
    row = await db_exec(
        "SELECT warns FROM users WHERE user_id=? AND chat_id=?",
        (user_id, chat_id), fetch="one",
    )
    return row["warns"] if row else 0


async def reset_warns(user_id: int, chat_id: int):
    await db_exec(
        "UPDATE users SET warns=0 WHERE user_id=? AND chat_id=?",
        (user_id, chat_id),
    )


async def set_mute(user_id: int, chat_id: int, until_iso: Optional[str]):
    await upsert_user(user_id, chat_id, None, None)
    await db_exec(
        "UPDATE users SET mute_until=? WHERE user_id=? AND chat_id=?",
        (until_iso, user_id, chat_id),
    )


async def add_trigger(chat_id: int, word: str, action: str):
    await db_exec(
        "INSERT INTO triggers (chat_id, word, action) VALUES (?, ?, ?)",
        (chat_id, word.lower(), action),
    )


async def del_trigger(chat_id: int, word: str):
    await db_exec(
        "DELETE FROM triggers WHERE chat_id=? AND word=?",
        (chat_id, word.lower()),
    )


async def list_triggers(chat_id: int):
    return await db_exec(
        "SELECT word, action FROM triggers WHERE chat_id=?",
        (chat_id,), fetch="all",
    ) or []


async def create_circle(chat_id: int, name: str, owner_id: int, thread_id: int) -> int:
    return await db_exec(
        "INSERT INTO circles (chat_id, name, owner_id, thread_id) VALUES (?, ?, ?, ?)",
        (chat_id, name, owner_id, thread_id),
    )


async def get_circle_by_name(chat_id: int, name: str):
    return await db_exec(
        "SELECT * FROM circles WHERE chat_id=? AND lower(name)=lower(?)",
        (chat_id, name), fetch="one",
    )


async def get_circle_by_thread(chat_id: int, thread_id: int):
    return await db_exec(
        "SELECT * FROM circles WHERE chat_id=? AND thread_id=?",
        (chat_id, thread_id), fetch="one",
    )


async def delete_circle(circle_id: int):
    await db_exec("DELETE FROM circles WHERE id=?", (circle_id,))
    await db_exec("DELETE FROM circle_members WHERE circle_id=?", (circle_id,))


async def add_circle_member(circle_id: int, user_id: int):
    await db_exec(
        "INSERT OR IGNORE INTO circle_members (circle_id, user_id) VALUES (?, ?)",
        (circle_id, user_id),
    )


async def remove_circle_member(circle_id: int, user_id: int):
    await db_exec(
        "DELETE FROM circle_members WHERE circle_id=? AND user_id=?",
        (circle_id, user_id),
    )


async def circle_members(circle_id: int):
    return await db_exec(
        "SELECT user_id FROM circle_members WHERE circle_id=?",
        (circle_id,), fetch="all",
    ) or []


async def circles_of_user(chat_id: int, user_id: int):
    rows = await db_exec(
        """
        SELECT c.id, c.name FROM circles c
        JOIN circle_members m ON m.circle_id = c.id
        WHERE c.chat_id=? AND m.user_id=?
        """,
        (chat_id, user_id), fetch="all",
    )
    return rows or []


async def ensure_topic(chat_id: int, thread_id: int):
    await db_exec(
        "INSERT OR IGNORE INTO topics (chat_id, thread_id) VALUES (?, ?)",
        (chat_id, thread_id),
    )


async def get_topic_links_allowed(chat_id: int, thread_id: int) -> bool:
    await ensure_topic(chat_id, thread_id)
    row = await db_exec(
        "SELECT links_allowed FROM topics WHERE chat_id=? AND thread_id=?",
        (chat_id, thread_id), fetch="one",
    )
    return bool(row["links_allowed"]) if row else True


async def set_topic_links(chat_id: int, thread_id: int, allowed: bool):
    await ensure_topic(chat_id, thread_id)
    await db_exec(
        "UPDATE topics SET links_allowed=? WHERE chat_id=? AND thread_id=?",
        (1 if allowed else 0, chat_id, thread_id),
    )


async def get_settings(chat_id: int):
    row = await db_exec("SELECT * FROM settings WHERE chat_id=?", (chat_id,), fetch="one")
    if row:
        return row
    await db_exec("INSERT OR IGNORE INTO settings (chat_id) VALUES (?)", (chat_id,))
    row = await db_exec("SELECT * FROM settings WHERE chat_id=?", (chat_id,), fetch="one")
    return row


def _s(row, key, default):
    try:
        v = row[key]
        return default if v is None else v
    except (IndexError, KeyError):
        return default


async def set_setting(chat_id: int, key: str, value):
    await db_exec("INSERT OR IGNORE INTO settings (chat_id) VALUES (?)", (chat_id,))
    await db_exec(f"UPDATE settings SET {key}=? WHERE chat_id=?", (value, chat_id))


async def create_report(chat_id, thread_id, reporter_id, target_id, reason, link) -> int:
    return await db_exec(
        """
        INSERT INTO reports (chat_id, thread_id, reporter_id, target_id, reason, msg_link)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (chat_id, thread_id, reporter_id, target_id, reason, link),
    )


async def close_report(report_id: int, status: str):
    await db_exec("UPDATE reports SET status=? WHERE id=?", (status, report_id))


async def remember_message(chat_id: int, message_id: int, thread_id: int, user_id: Optional[int]):
    await db_exec(
        "INSERT OR REPLACE INTO messages (chat_id, message_id, thread_id, user_id) VALUES (?, ?, ?, ?)",
        (chat_id, message_id, thread_id, user_id),
    )


async def last_messages(chat_id: int, thread_id: int, limit: int):
    return await db_exec(
        "SELECT message_id FROM messages WHERE chat_id=? AND thread_id=? ORDER BY message_id DESC LIMIT ?",
        (chat_id, thread_id, limit), fetch="all",
    ) or []


async def forget_messages(chat_id: int, message_ids: list):
    if not message_ids:
        return
    q = ",".join("?" * len(message_ids))
    await db_exec(f"DELETE FROM messages WHERE chat_id=? AND message_id IN ({q})",
                  (chat_id, *message_ids))


async def add_blacklist(user_id: int, chat_id: int, reason: str):
    await db_exec(
        """
        INSERT INTO blacklist (user_id, chat_id, reason)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id, chat_id) DO UPDATE SET reason=excluded.reason
        """,
        (user_id, chat_id, reason),
    )


async def remove_blacklist(user_id: int, chat_id: int):
    await db_exec("DELETE FROM blacklist WHERE user_id=? AND chat_id=?", (user_id, chat_id))


async def resolve_rank(bot, chat_id: int, user_id: int) -> int:
    db_rank = await get_rank(user_id, chat_id)
    if db_rank >= RANK_OWNER:
        return db_rank
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except Exception:
        return db_rank
    if member.status == "creator":
        return max(db_rank, RANK_OWNER)
    if member.status == "administrator":
        return max(db_rank, RANK_SENIOR_ADMIN)
    return db_rank


async def can_act_on(bot, chat_id: int, actor_id: int, target_id: int) -> bool:
    actor_rank = await resolve_rank(bot, chat_id, actor_id)
    target_rank = await resolve_rank(bot, chat_id, target_id)
    return actor_rank > target_rank


async def _rank_error(update: Update, min_rank: int, rank: int):
    msg = update.effective_message
    if msg is None:
        return
    await eph(
        msg,
        f"⛔ Недостаточно прав.\n"
        f"Нужен: <b>{RANK_NAMES.get(min_rank, '—')}</b>\n"
        f"У тебя: <b>{RANK_NAMES.get(rank, '—')}</b>",
        parse_mode=ParseMode.HTML,
    )


async def _deny_higher(msg, target_mention: str, target_rank: int = -1):
    if target_rank == RANK_OWNER:
        extra = "👑 <b>Создателя</b> наказать нельзя."
    elif target_rank == RANK_SENIOR_ADMIN:
        extra = "🛡 <b>Старшего администратора</b> наказать нельзя."
    else:
        rn = RANK_NAMES.get(target_rank, "—") if target_rank >= 0 else "—"
        extra = f"его ранг: <b>{rn}</b> — не ниже твоего."
    await eph(
        msg,
        f"⛔ Нельзя наказать: {target_mention}\n{extra}",
        parse_mode=ParseMode.HTML,
    )


async def _no_target(msg):
    await eph(
        msg,
        "❌ Вы никого не указали.\n\n"
        "Ответьте <b>реплаем</b> на сообщение нарушителя и повторите команду.\n"
        "Свайп по его сообщению вправо → появится полоска «Ответить».",
        parse_mode=ParseMode.HTML,
    )


async def _mute_user(bot, chat_id: int, user_id: int, secs: int) -> bool:
    secs = max(1, int(secs))
    try:
        if secs >= MIN_TG_MUTE_SEC:
            await bot.restrict_chat_member(
                chat_id, user_id, MUTE_PERMS,
                until_date=int(time.time()) + secs,
            )
        else:
            await bot.restrict_chat_member(chat_id, user_id, MUTE_PERMS)
            _schedule(_unmute_later(bot, chat_id, user_id, secs))
        await set_mute(user_id, chat_id, None)
        return True
    except Exception as e:
        log.debug("_mute_user fail: %s", e)
        return False


async def _unmute_later(bot, chat_id: int, user_id: int, secs: int):
    await asyncio.sleep(secs)
    try:
        await bot.restrict_chat_member(chat_id, user_id, UNMUTE_PERMS)
        await set_mute(user_id, chat_id, None)
    except Exception as e:
        log.debug("auto-unmute fail: %s", e)


def mod_action(fn):
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        msg = update.effective_message
        try:
            await fn(update, ctx)
        finally:
            if msg is not None and msg.chat.type != "private":
                await _delayed(ctx.bot, msg.chat.id, msg.message_id)
    return wrapper


def mod_kb(chat_id: int, user_id: int, report_id: Optional[int] = None) -> InlineKeyboardMarkup:
    tail = f":{report_id}" if report_id else ""
    rows = [
        [
            InlineKeyboardButton("⚠️ Варн",   callback_data=f"mod:warn:{chat_id}:{user_id}{tail}"),
            InlineKeyboardButton("🔇 Мут",     callback_data=f"mod:mute:{chat_id}:{user_id}{tail}"),
            InlineKeyboardButton("👢 Кик",     callback_data=f"mod:kick:{chat_id}:{user_id}{tail}"),
        ],
        [
            InlineKeyboardButton("🔨 Бан",     callback_data=f"mod:ban:{chat_id}:{user_id}{tail}"),
            InlineKeyboardButton("🔊 Размут",  callback_data=f"mod:unmute:{chat_id}:{user_id}{tail}"),
            InlineKeyboardButton("♻️ Анварн",  callback_data=f"mod:unwarn:{chat_id}:{user_id}{tail}"),
        ],
        [
            InlineKeyboardButton("🔓 Разбан",  callback_data=f"mod:unban:{chat_id}:{user_id}{tail}"),
            InlineKeyboardButton("📄 Профиль", callback_data=f"mod:profile:{chat_id}:{user_id}{tail}"),
        ],
    ]
    if report_id:
        rows.append([
            InlineKeyboardButton("✅ Закрыть", callback_data=f"mod:close:{chat_id}:{user_id}{tail}"),
        ])
    return InlineKeyboardMarkup(rows)


_flood_buckets: dict = defaultdict(deque)
_raid_buckets:  dict = defaultdict(deque)
_raid_alerted:  dict = {}


async def private_block_mw(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if msg is None:
        return
    if msg.chat.type != "private":
        return
    await safe(
        msg.reply_text,
        "⛔ У вас недостаточно прав, для обновления прав обратитесь к техническому специалисту",
    )
    raise ApplicationHandlerStop


async def pre_remember(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if msg is None or msg.chat.type == "private":
        return
    if msg.from_user:
        await upsert_user(msg.from_user.id, msg.chat.id,
                          msg.from_user.username, msg.from_user.first_name)
    await remember_message(
        msg.chat.id, msg.message_id,
        msg.message_thread_id or 0,
        msg.from_user.id if msg.from_user else None,
    )


async def antispam_mw(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    if msg is None or user is None or msg.chat.type == "private":
        return

    settings = await get_settings(msg.chat.id)
    if not settings or not settings["antispam"]:
        return

    rank = await resolve_rank(ctx.bot, msg.chat.id, user.id)
    if rank >= RANK_SENIOR_MOD:
        return

    key = (msg.chat.id, user.id)
    now = time.monotonic()
    bucket = _flood_buckets[key]
    bucket.append(now)
    while bucket and now - bucket[0] > settings["flood_window"]:
        bucket.popleft()

    if len(bucket) >= settings["flood_limit"]:
        mute_sec = int(_s(settings, "flood_mute_sec", 1800))
        await _mute_user(ctx.bot, msg.chat.id, user.id, mute_sec)
        await eph(
            msg,
            f"🔇 {user.mention_html()} — мут за флуд на {fmt_seconds(mute_sec)}.",
            parse_mode=ParseMode.HTML,
        )
        await log_action(ctx.bot, None, f"🔇 Авто-мут за флуд {fmt_seconds(mute_sec)}", user, "",
                         msg.chat.id, reply_msg_id=msg.message_id)
        bucket.clear()
        raise ApplicationHandlerStop


async def link_guard_mw(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    if msg is None or user is None or msg.chat.type == "private":
        return

    text = msg.text or msg.caption or ""
    if not text or not LINK_RE.search(text):
        return

    settings = await get_settings(msg.chat.id)
    links_forbidden = int(_s(settings, "links_forbidden", 0)) == 1

    thread_id = msg.message_thread_id or 0
    topic_allowed = await get_topic_links_allowed(msg.chat.id, thread_id)

    if not links_forbidden and topic_allowed:
        return

    rank = await resolve_rank(ctx.bot, msg.chat.id, user.id)
    if rank >= RANK_SENIOR_MOD:
        return

    await safe(msg.delete)
    note = await eph(
        msg,
        "🚫 Ссылки в этом чате запрещены.",
        disable_notification=True,
    )
    raise ApplicationHandlerStop


async def trigger_mw(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    if msg is None or user is None or msg.chat.type == "private":
        return

    text = (msg.text or msg.caption or "").lower()
    if not text:
        return

    settings = await get_settings(msg.chat.id)
    if not settings or not settings["triggers_on"]:
        return

    triggers = await list_triggers(msg.chat.id)
    if not triggers:
        return

    rank = await resolve_rank(ctx.bot, msg.chat.id, user.id)
    if rank >= RANK_SENIOR_MOD:
        return

    for row in triggers:
        word = (row["word"] or "").strip().lower()
        if not word:
            continue
        if word in text:
            action = row["action"]
            await safe(msg.delete)
            await _apply_trigger(ctx.bot, msg.chat.id, user, action, word, settings)
            raise ApplicationHandlerStop


async def _apply_trigger(bot, chat_id: int, user, action: str, word: str, settings):
    uid = user.id
    mention = user.mention_html()
    trig_warn_limit = int(_s(settings, "trig_warn_limit", 3))
    trig_mute_sec = int(_s(settings, "trig_mute_sec", 3600))
    auto_mute_sec = int(_s(settings, "auto_mute_sec", 3600))

    if action == "warn":
        total = await add_warn(uid, chat_id)
        note = await safe(
            bot.send_message, chat_id,
            f"⚠️ {mention} — предупреждение (триггер: <code>{esc(word)}</code>). Всего: {total}/{trig_warn_limit}",
            parse_mode=ParseMode.HTML,
        )
        if note is not None:
            await _delayed(bot, chat_id, note.message_id)
        await log_action(bot, None, f"⚠️ Авто-варн (триггер: {word}) {total}/{trig_warn_limit}",
                         user, "", chat_id)
        if total >= trig_warn_limit:
            warn_action = str(_s(settings, "warn_action", "ban")).lower()
            try:
                if warn_action == "ban":
                    await bot.ban_chat_member(chat_id, uid)
                    await add_blacklist(uid, chat_id, "auto_ban_warns")
                    await reset_warns(uid, chat_id)
                    note2 = await safe(bot.send_message, chat_id,
                                       f"🔨 {mention} — забанен ({trig_warn_limit}/{trig_warn_limit} варнов).",
                                       parse_mode=ParseMode.HTML)
                    if note2 is not None:
                        await _delayed(bot, chat_id, note2.message_id)
                    await log_action(bot, None,
                                     f"🔨 Авто-бан ({trig_warn_limit}/{trig_warn_limit} варнов по триггеру)",
                                     user, "", chat_id)
                elif warn_action == "kick":
                    await bot.ban_chat_member(chat_id, uid)
                    await bot.unban_chat_member(chat_id, uid)
                    await reset_warns(uid, chat_id)
                    note2 = await safe(bot.send_message, chat_id,
                                       f"👢 {mention} — кикнут ({trig_warn_limit}/{trig_warn_limit} варнов).",
                                       parse_mode=ParseMode.HTML)
                    if note2 is not None:
                        await _delayed(bot, chat_id, note2.message_id)
                    await log_action(bot, None,
                                     f"👢 Авто-кик ({trig_warn_limit}/{trig_warn_limit} варнов по триггеру)",
                                     user, "", chat_id)
                else:
                    await _mute_user(bot, chat_id, uid, auto_mute_sec)
                    await reset_warns(uid, chat_id)
                    note2 = await safe(bot.send_message, chat_id,
                                       f"🔇 {mention} — авто-мут на {fmt_seconds(auto_mute_sec)} ({trig_warn_limit}/{trig_warn_limit}).",
                                       parse_mode=ParseMode.HTML)
                    if note2 is not None:
                        await _delayed(bot, chat_id, note2.message_id)
                    await log_action(bot, None,
                                     f"🔇 Авто-мут {fmt_seconds(auto_mute_sec)} ({trig_warn_limit}/{trig_warn_limit} варна)",
                                     user, "", chat_id)
            except Exception as e:
                log.debug("auto-warn-action fail (trigger): %s", e)
    elif action == "mute":
        await _mute_user(bot, chat_id, uid, trig_mute_sec)
        await log_action(bot, None, f"🔇 Авто-мут {fmt_seconds(trig_mute_sec)} (триггер: {word})",
                         user, "", chat_id)
    elif action == "kick":
        try:
            await bot.ban_chat_member(chat_id, uid)
            await bot.unban_chat_member(chat_id, uid)
            await log_action(bot, None, f"👢 Авто-кик (триггер: {word})", user, "", chat_id)
        except Exception as e:
            log.debug("kick fail: %s", e)
    elif action == "ban":
        try:
            await bot.ban_chat_member(chat_id, uid)
            await log_action(bot, None, f"🔨 Авто-бан (триггер: {word})", user, "", chat_id)
        except Exception as e:
            log.debug("ban fail: %s", e)


async def antiraid_track(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cm = update.chat_member
    if cm is None:
        return

    me = await ctx.bot.get_me()
    if cm.new_chat_member.user.id == me.id:
        return

    chat_id = cm.chat.id
    settings = await get_settings(chat_id)
    if not settings or not settings["antiraid"]:
        return

    antiraid_joins = int(_s(settings, "antiraid_joins", 10))
    antiraid_window = float(_s(settings, "antiraid_window", 30))
    antiraid_lookback = float(_s(settings, "antiraid_lookback", 1800))

    old_status = cm.old_chat_member.status
    new_status = cm.new_chat_member.status
    if old_status in ("left", "kicked") and new_status in ("member", "restricted"):
        now = time.monotonic()
        bucket = _raid_buckets[chat_id]
        bucket.append(now)
        while bucket and now - bucket[0] > antiraid_window:
            bucket.popleft()

        if len(bucket) >= antiraid_joins:
            last_alert = _raid_alerted.get(chat_id, 0)
            if now - last_alert < antiraid_lookback:
                return
            _raid_alerted[chat_id] = now
            await send_admin(
                ctx.bot,
                f"🚨 <b>Возможный рейд</b> в чате <code>{chat_id}</code>\n"
                f"За <b>{int(antiraid_window)}</b> сек вошло <b>{len(bucket)}</b> новых участников.",
                parse_mode=ParseMode.HTML,
            )


async def on_bot_added(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cm = update.chat_member
    if cm is None:
        return

    me = await ctx.bot.get_me()
    if cm.new_chat_member.user.id != me.id:
        return

    old_status = cm.old_chat_member.status
    new_status = cm.new_chat_member.status

    became_in = (
        old_status in ("left", "kicked") and new_status in ("member", "administrator", "restricted")
    ) or (old_status == "member" and new_status == "administrator")

    if not became_in:
        return

    adder = cm.from_user
    if adder is None or adder.is_bot:
        log.warning("Не смог определить, кто добавил бота в %s", cm.chat.id)
        return

    current = await get_rank(adder.id, cm.chat.id)
    if current >= RANK_OWNER:
        return

    await set_rank(adder.id, cm.chat.id, RANK_OWNER)
    log.info("Bot added to %s by %s (%s) → OWNER",
             cm.chat.id, adder.id, adder.username or adder.first_name)

    await safe(
        ctx.bot.send_message,
        cm.chat.id,
        f"👑 {adder.mention_html()} — теперь <b>Создатель</b> этого чата "
        f"(добавил бота).\n"
        f"Список команд: <code>помощь</code>",
        parse_mode=ParseMode.HTML,
    )


HELP_HEADER = "🛠 <b>Модератор-бот</b>\n\n"

HELP_BLOCKS = {
    "ranks_owner": (
        "<b>Управление рангами:</b>\n"
        "• <code>выдатьранг алиас</code> — реплай\n"
        "• <code>снятьранг</code> — реплай\n"
        "• <code>роли</code> — все с рангами\n"
        "• <code>ранги</code> — список алиасов\n\n",
        RANK_OWNER,
    ),
    "settings": (
        "<b>Настройки:</b>\n"
        "• <code>настройки</code>\n\n",
        RANK_SENIOR_ADMIN,
    ),
    "mod_junior_admin": (
        "<b>Админ-команды (реплай):</b>\n"
        "• <code>бан [причина]</code>\n"
        "• <code>разбан</code> — реплай или <code>разбан ID</code>\n"
        "• <code>роли</code> — все с рангами\n\n",
        RANK_JUNIOR_ADMIN,
    ),
    "mod_junior_mod": (
        "<b>Модерация (реплай):</b>\n"
        "• <code>мод</code> — меню кнопок\n"
        "• <code>кик</code>\n"
        "• <code>мут 10м</code>\n"
        "• <code>размут</code>\n"
        "• <code>варн</code>\n"
        "• <code>анварн</code>\n"
        "• <code>варны</code>\n"
        "• <code>чистка N</code>\n"
        "• <code>ранги</code> — список рангов\n\n",
        RANK_JUNIOR_MOD,
    ),
    "mod_senior_mod": (
        "<b>Старший модератор:</b>\n"
        "• <code>триггер добавить слово [варн|мут|кик|бан]</code>\n"
        "• <code>триггер удалить слово</code>\n"
        "• <code>триггер список</code>\n"
        "• <code>ссылки вкл</code> или <code>ссылки выкл</code>\n"
        "• <code>создатькружок имя</code>\n"
        "• <code>удалитькружок имя</code>\n\n",
        RANK_SENIOR_MOD,
    ),
    "everyone": (
        "<b>Доступно всем:</b>\n"
        "• <code>репорт причина</code> — жалоба (реплай)\n"
        "• <code>профиль</code>\n"
        "• <code>я</code>\n"
        "• <code>вступить имя</code>\n"
        "• <code>выйти имя</code>\n"
        "• <code>инфокружка имя</code>\n"
        "• <code>помощь</code>\n",
        RANK_USER,
    ),
}


async def build_help(bot, chat_id: int, user_id: int) -> str:
    rank = await resolve_rank(bot, chat_id, user_id)
    lines = [HELP_HEADER]
    lines.append(f"Твой ранг: <b>{RANK_NAMES.get(rank, '—')}</b>\n\n")
    for _, (text, min_rank) in HELP_BLOCKS.items():
        if rank >= min_rank:
            lines.append(text)
    return "".join(lines)


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    if msg is None or user is None:
        return
    text = await build_help(ctx.bot, msg.chat.id, user.id)
    await eph(msg, text, parse_mode=ParseMode.HTML,
              disable_web_page_preview=True)


@mod_action
async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    if msg is None or user is None:
        return
    text = await build_help(ctx.bot, msg.chat.id, user.id)
    await eph(msg, text, parse_mode=ParseMode.HTML,
              disable_web_page_preview=True)


@mod_action
async def cmd_ranks(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    if msg is None or user is None or msg.chat.type == "private":
        return
    my_rank = await resolve_rank(ctx.bot, msg.chat.id, user.id)
    if my_rank < RANK_JUNIOR_MOD:
        await _rank_error(update, RANK_JUNIOR_MOD, my_rank)
        return
    await eph(
        msg,
        "<b>Доступные ранги:</b>\n" + RANK_HINTS,
        parse_mode=ParseMode.HTML,
    )


@mod_action
async def cmd_roles(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    if msg is None or user is None or msg.chat.type == "private":
        return

    my_rank = await resolve_rank(ctx.bot, msg.chat.id, user.id)
    if my_rank < RANK_SENIOR_ADMIN:
        await _rank_error(update, RANK_SENIOR_ADMIN, my_rank)
        return

    chat_id = msg.chat.id
    entries: dict = {}

    rows = await db_exec(
        "SELECT user_id, username, first_name, rank FROM users WHERE chat_id=? AND rank>0",
        (chat_id,), fetch="all",
    ) or []
    for row in rows:
        uid = row["user_id"]
        entries[uid] = [row["rank"], row["username"] or row["first_name"] or ""]

    try:
        admins = await ctx.bot.get_chat_administrators(chat_id)
        for a in admins:
            u = a.user
            if u is None or u.is_bot:
                continue
            if a.status == "creator":
                r = RANK_OWNER
            elif a.status == "administrator":
                r = RANK_SENIOR_ADMIN
            else:
                continue
            if u.id in entries:
                entries[u.id][0] = max(entries[u.id][0], r)
            else:
                entries[u.id] = [r, u.username or u.first_name or ""]
    except Exception as e:
        log.debug("get_chat_administrators fail: %s", e)

    if not entries:
        await eph(msg, "📭 Пока ни у кого нет ролей.")
        return

    by_rank: dict = defaultdict(list)
    for uid, (r, name) in entries.items():
        by_rank[r].append((uid, name))

    lines = ["<b>👥 Роли в этом чате</b>"]
    for r in sorted(by_rank.keys(), reverse=True):
        lines.append(f"\n<b>{RANK_NAMES.get(r, '—')}</b>")
        for uid, name in by_rank[r]:
            if name:
                lines.append(f"• {esc(name)} — <code>{uid}</code>")
            else:
                lines.append(f"• <code>{uid}</code>")

    full = "\n".join(lines)

    if len(full) <= 3800:
        await eph(msg, full, parse_mode=ParseMode.HTML,
                  disable_web_page_preview=True)
        return

    chunk = ""
    for line in lines:
        if len(chunk) + len(line) + 1 > 3800:
            await eph(msg, chunk, parse_mode=ParseMode.HTML,
                      disable_web_page_preview=True)
            chunk = ""
        chunk += line + "\n"
    if chunk.strip():
        await eph(msg, chunk, parse_mode=ParseMode.HTML,
                  disable_web_page_preview=True)


@mod_action
async def cmd_setrank(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    if msg is None or user is None or msg.chat.type == "private":
        return

    my_rank = await resolve_rank(ctx.bot, msg.chat.id, user.id)
    if my_rank < RANK_OWNER:
        await _rank_error(update, RANK_OWNER, my_rank)
        return

    if msg.reply_to_message is None or msg.reply_to_message.from_user is None:
        await eph(
            msg,
            "❌ Нужно ответить на сообщение того, кому выдаёшь ранг.\n\n"
            "Пример: ответь на сообщение и напиши <code>выдатьранг мл_мод</code>\n\n"
            "Доступные ранги:\n" + RANK_HINTS,
            parse_mode=ParseMode.HTML,
        )
        return

    parts = (msg.text or "").split()
    if len(parts) < 2:
        await eph(
            msg,
            "❌ Не указан ранг.\n\n"
            "Пример: <code>выдатьранг мл_мод</code>\n\n"
            "Доступные ранги:\n" + RANK_HINTS,
            parse_mode=ParseMode.HTML,
        )
        return

    key = parts[1].lower().strip()
    if key not in RANK_ALIASES:
        await eph(
            msg,
            f"❌ Ранг <code>{esc(key)}</code> не найден.\n\n"
            "Доступные ранги:\n" + RANK_HINTS,
            parse_mode=ParseMode.HTML,
        )
        return

    target = msg.reply_to_message.from_user
    rank = RANK_ALIASES[key]
    await set_rank(target.id, msg.chat.id, rank)
    await log_action(ctx.bot, user, f"👑 Выдача ранга: {RANK_NAMES[rank]}", target, "",
                     msg.chat.id, reply_msg_id=msg.reply_to_message.message_id)
    await eph(
        msg,
        f"✅ Выдано: {target.mention_html()} → <b>{RANK_NAMES[rank]}</b>",
        parse_mode=ParseMode.HTML,
    )


@mod_action
async def cmd_unrank(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    if msg is None or user is None or msg.chat.type == "private":
        return

    my_rank = await resolve_rank(ctx.bot, msg.chat.id, user.id)
    if my_rank < RANK_OWNER:
        await _rank_error(update, RANK_OWNER, my_rank)
        return

    if msg.reply_to_message is None or msg.reply_to_message.from_user is None:
        await eph(msg, "❌ Ответь на сообщение того, у кого снять ранг.",
                  parse_mode=ParseMode.HTML)
        return

    target = msg.reply_to_message.from_user
    await set_rank(target.id, msg.chat.id, RANK_USER)
    await log_action(ctx.bot, user, "👑 Снятие ранга", target, "", msg.chat.id,
                     reply_msg_id=msg.reply_to_message.message_id)
    await eph(
        msg,
        f"✅ Снято: {target.mention_html()} → <b>{RANK_NAMES[RANK_USER]}</b>",
        parse_mode=ParseMode.HTML,
    )


async def _target_user(msg):
    if not msg.reply_to_message or not msg.reply_to_message.from_user:
        return None
    t = msg.reply_to_message.from_user
    if msg.from_user and t.id == msg.from_user.id:
        return None
    return t


@mod_action
async def cmd_ban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message; user = update.effective_user
    if msg is None or user is None or msg.chat.type == "private":
        return
    my_rank = await resolve_rank(ctx.bot, msg.chat.id, user.id)
    if my_rank < RANK_JUNIOR_ADMIN:
        await _rank_error(update, RANK_JUNIOR_ADMIN, my_rank); return
    t = await _target_user(msg)
    if not t:
        await _no_target(msg); return
    if not await can_act_on(ctx.bot, msg.chat.id, user.id, t.id):
        tr = await resolve_rank(ctx.bot, msg.chat.id, t.id)
        await _deny_higher(msg, t.mention_html(), tr); return
    reason = " ".join((msg.text or "").split()[1:]) or "—"
    reply_id = msg.reply_to_message.message_id if msg.reply_to_message else None
    try:
        await ctx.bot.ban_chat_member(msg.chat.id, t.id)
        await add_blacklist(t.id, msg.chat.id, "banned")
        await log_action(ctx.bot, user, "🔨 Бан", t, reason, msg.chat.id,
                         reply_msg_id=reply_id)
        await eph(msg, f"🔨 Готово: {t.mention_html()} забанен.\nПричина: {esc(reason)}",
                  parse_mode=ParseMode.HTML)
    except Exception as e:
        await eph(msg, f"❌ Ошибка: {esc(e)}")


@mod_action
async def cmd_unban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message; user = update.effective_user
    if msg is None or user is None or msg.chat.type == "private":
        return
    my_rank = await resolve_rank(ctx.bot, msg.chat.id, user.id)
    if my_rank < RANK_JUNIOR_ADMIN:
        await _rank_error(update, RANK_JUNIOR_ADMIN, my_rank); return

    parts = (msg.text or "").split()

    uid = None
    if msg.reply_to_message and msg.reply_to_message.from_user:
        uid = msg.reply_to_message.from_user.id
    elif len(parts) >= 2 and parts[1].lstrip("-").isdigit():
        uid = int(parts[1])

    if uid is None:
        await eph(
            msg,
            "❌ Вы никого не указали.\n\n"
            "Как разбанить:\n"
            "• <b>реплаем</b> на любое сообщение нарушителя и напиши <code>разбан</code>\n"
            "• или укажи ID: <code>разбан 123456789</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        await ctx.bot.unban_chat_member(msg.chat.id, uid)
        await remove_blacklist(uid, msg.chat.id)
        await log_action(ctx.bot, user, "🔓 Разбан", uid, "", msg.chat.id,
                         reply_msg_id=msg.reply_to_message.message_id if msg.reply_to_message else None)
        await eph(msg, f"✅ Готово: разбанен <code>{uid}</code>.",
                  parse_mode=ParseMode.HTML)
    except Exception as e:
        await eph(msg, f"❌ Ошибка: {esc(e)}")


@mod_action
async def cmd_kick(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message; user = update.effective_user
    if msg is None or user is None or msg.chat.type == "private":
        return
    my_rank = await resolve_rank(ctx.bot, msg.chat.id, user.id)
    if my_rank < RANK_JUNIOR_MOD:
        await _rank_error(update, RANK_JUNIOR_MOD, my_rank); return
    t = await _target_user(msg)
    if not t:
        await _no_target(msg); return
    if not await can_act_on(ctx.bot, msg.chat.id, user.id, t.id):
        tr = await resolve_rank(ctx.bot, msg.chat.id, t.id)
        await _deny_higher(msg, t.mention_html(), tr); return
    reply_id = msg.reply_to_message.message_id if msg.reply_to_message else None
    try:
        await ctx.bot.ban_chat_member(msg.chat.id, t.id)
        await ctx.bot.unban_chat_member(msg.chat.id, t.id)
        await log_action(ctx.bot, user, "👢 Кик", t, "", msg.chat.id, reply_msg_id=reply_id)
        await eph(msg, f"👢 Готово: {t.mention_html()} кикнут.",
                  parse_mode=ParseMode.HTML)
    except Exception as e:
        await eph(msg, f"❌ Ошибка: {esc(e)}")


@mod_action
async def cmd_mute(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message; user = update.effective_user
    if msg is None or user is None or msg.chat.type == "private":
        return
    my_rank = await resolve_rank(ctx.bot, msg.chat.id, user.id)
    if my_rank < RANK_JUNIOR_MOD:
        await _rank_error(update, RANK_JUNIOR_MOD, my_rank); return
    t = await _target_user(msg)
    if not t:
        await _no_target(msg); return
    if not await can_act_on(ctx.bot, msg.chat.id, user.id, t.id):
        tr = await resolve_rank(ctx.bot, msg.chat.id, t.id)
        await _deny_higher(msg, t.mention_html(), tr); return
    parts = (msg.text or "").split()
    if len(parts) > 1:
        secs = parse_duration(parts[1])
        if secs <= 0:
            secs = parse_setting_time(parts[1])
    else:
        settings = await get_settings(msg.chat.id)
        secs = int(_s(settings, "default_mute_sec", 600))
    if secs <= 0:
        settings = await get_settings(msg.chat.id)
        secs = int(_s(settings, "default_mute_sec", 600))
    reply_id = msg.reply_to_message.message_id if msg.reply_to_message else None
    ok = await _mute_user(ctx.bot, msg.chat.id, t.id, secs)
    if ok:
        await log_action(ctx.bot, user, f"🔇 Мут на {fmt_seconds(secs)}", t, "", msg.chat.id,
                         reply_msg_id=reply_id)
        await eph(msg, f"🔇 Готово: {t.mention_html()} замучен на {fmt_seconds(secs)}.",
                  parse_mode=ParseMode.HTML)
    else:
        await eph(msg, "❌ Не удалось замутить.")


@mod_action
async def cmd_unmute(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message; user = update.effective_user
    if msg is None or user is None or msg.chat.type == "private":
        return
    my_rank = await resolve_rank(ctx.bot, msg.chat.id, user.id)
    if my_rank < RANK_JUNIOR_MOD:
        await _rank_error(update, RANK_JUNIOR_MOD, my_rank); return
    t = await _target_user(msg)
    if not t:
        await _no_target(msg); return
    if not await can_act_on(ctx.bot, msg.chat.id, user.id, t.id):
        tr = await resolve_rank(ctx.bot, msg.chat.id, t.id)
        await _deny_higher(msg, t.mention_html(), tr); return
    reply_id = msg.reply_to_message.message_id if msg.reply_to_message else None
    try:
        await ctx.bot.restrict_chat_member(msg.chat.id, t.id, UNMUTE_PERMS)
        await set_mute(t.id, msg.chat.id, None)
        await log_action(ctx.bot, user, "🔊 Размут", t, "", msg.chat.id, reply_msg_id=reply_id)
        await eph(msg, f"🔊 Готово: {t.mention_html()} размучен.",
                  parse_mode=ParseMode.HTML)
    except Exception as e:
        await eph(msg, f"❌ Ошибка: {esc(e)}")


@mod_action
async def cmd_warn(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message; user = update.effective_user
    if msg is None or user is None or msg.chat.type == "private":
        return
    my_rank = await resolve_rank(ctx.bot, msg.chat.id, user.id)
    if my_rank < RANK_JUNIOR_MOD:
        await _rank_error(update, RANK_JUNIOR_MOD, my_rank); return
    t = await _target_user(msg)
    if not t:
        await _no_target(msg); return
    if not await can_act_on(ctx.bot, msg.chat.id, user.id, t.id):
        tr = await resolve_rank(ctx.bot, msg.chat.id, t.id)
        await _deny_higher(msg, t.mention_html(), tr); return
    settings = await get_settings(msg.chat.id)
    warn_limit = int(_s(settings, "warn_limit", 3))
    auto_mute_sec = int(_s(settings, "auto_mute_sec", 3600))
    reply_id = msg.reply_to_message.message_id if msg.reply_to_message else None
    total = await add_warn(t.id, msg.chat.id)
    await log_action(ctx.bot, user, f"⚠️ Варн ({total}/{warn_limit})", t, "", msg.chat.id,
                     reply_msg_id=reply_id)
    await eph(msg, f"⚠️ Готово: {t.mention_html()} — предупреждение ({total}/{warn_limit}).",
              parse_mode=ParseMode.HTML)
    if total >= warn_limit:
        warn_action = str(_s(settings, "warn_action", "ban")).lower()
        try:
            if warn_action == "ban":
                await ctx.bot.ban_chat_member(msg.chat.id, t.id)
                await add_blacklist(t.id, msg.chat.id, "auto_ban_warns")
                await reset_warns(t.id, msg.chat.id)
                await log_action(ctx.bot, user,
                                 f"🔨 Авто-бан ({warn_limit}/{warn_limit} варнов)",
                                 t, "", msg.chat.id, reply_msg_id=reply_id)
                await eph(msg, f"🔨 {t.mention_html()} — забанен ({warn_limit}/{warn_limit} варнов).",
                          parse_mode=ParseMode.HTML)
            elif warn_action == "kick":
                await ctx.bot.ban_chat_member(msg.chat.id, t.id)
                await ctx.bot.unban_chat_member(msg.chat.id, t.id)
                await reset_warns(t.id, msg.chat.id)
                await log_action(ctx.bot, user,
                                 f"👢 Авто-кик ({warn_limit}/{warn_limit} варнов)",
                                 t, "", msg.chat.id, reply_msg_id=reply_id)
                await eph(msg, f"👢 {t.mention_html()} — кикнут ({warn_limit}/{warn_limit} варнов).",
                          parse_mode=ParseMode.HTML)
            else:
                ok = await _mute_user(ctx.bot, msg.chat.id, t.id, auto_mute_sec)
                if ok:
                    await reset_warns(t.id, msg.chat.id)
                    await log_action(ctx.bot, user,
                                     f"🔇 Авто-мут {fmt_seconds(auto_mute_sec)} ({warn_limit}/{warn_limit})",
                                     t, "", msg.chat.id, reply_msg_id=reply_id)
                    await eph(msg, f"🔇 {t.mention_html()} — авто-мут на {fmt_seconds(auto_mute_sec)} ({warn_limit}/{warn_limit}).",
                              parse_mode=ParseMode.HTML)
        except Exception as e:
            log.debug("auto-warn-action fail: %s", e)


@mod_action
async def cmd_unwarn(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message; user = update.effective_user
    if msg is None or user is None or msg.chat.type == "private":
        return
    my_rank = await resolve_rank(ctx.bot, msg.chat.id, user.id)
    if my_rank < RANK_JUNIOR_MOD:
        await _rank_error(update, RANK_JUNIOR_MOD, my_rank); return
    t = await _target_user(msg)
    if not t:
        await _no_target(msg); return
    if not await can_act_on(ctx.bot, msg.chat.id, user.id, t.id):
        tr = await resolve_rank(ctx.bot, msg.chat.id, t.id)
        await _deny_higher(msg, t.mention_html(), tr); return
    reply_id = msg.reply_to_message.message_id if msg.reply_to_message else None
    await reset_warns(t.id, msg.chat.id)
    await log_action(ctx.bot, user, "♻️ Сброс варнов", t, "", msg.chat.id, reply_msg_id=reply_id)
    await eph(msg, f"♻️ Готово: предупреждения {t.mention_html()} сброшены.",
              parse_mode=ParseMode.HTML)


@mod_action
async def cmd_warns(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message; user = update.effective_user
    if msg is None or user is None:
        return
    settings = await get_settings(msg.chat.id)
    warn_limit = int(_s(settings, "warn_limit", 3))
    t = await _target_user(msg) or user
    row = await get_user(t.id, msg.chat.id)
    warns = row["warns"] if row else 0
    await eph(msg, f"⚠️ {t.mention_html()}: {warns}/{warn_limit} предупреждений.",
              parse_mode=ParseMode.HTML)


@mod_action
async def cmd_mod(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message; user = update.effective_user
    if msg is None or user is None or msg.chat.type == "private":
        return
    my_rank = await resolve_rank(ctx.bot, msg.chat.id, user.id)
    if my_rank < RANK_JUNIOR_MOD:
        await _rank_error(update, RANK_JUNIOR_MOD, my_rank); return
    t = await _target_user(msg)
    if not t:
        await _no_target(msg); return
    sent = await safe(
        msg.reply_text,
        f"🛡 <b>Модерация</b>\n"
        f"Цель: {t.mention_html()} (<code>{t.id}</code>)",
        parse_mode=ParseMode.HTML,
        reply_markup=mod_kb(msg.chat.id, t.id),
    )
    if sent is not None:
        await _delayed(ctx.bot, msg.chat.id, sent.message_id)


async def cb_mod(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q is None or q.message is None:
        return
    parts = q.data.split(":")
    action = parts[1]
    chat_id = int(parts[2])
    target_id = int(parts[3])
    report_id = int(parts[4]) if len(parts) > 4 else None

    required = RANK_JUNIOR_ADMIN if action in ("ban", "kick") else RANK_JUNIOR_MOD
    rank_here = await resolve_rank(ctx.bot, q.message.chat.id, q.from_user.id)
    rank_target_chat = await resolve_rank(ctx.bot, chat_id, q.from_user.id)
    my_rank = max(rank_here, rank_target_chat)
    if my_rank < required:
        await safe(q.answer,
                   f"⛔ Нужен {RANK_NAMES.get(required)}", show_alert=True)
        return

    if action in ("ban", "kick", "mute", "warn", "unmute", "unwarn"):
        if not await can_act_on(ctx.bot, chat_id, q.from_user.id, target_id):
            try:
                tr = await resolve_rank(ctx.bot, chat_id, target_id)
                rn = RANK_NAMES.get(tr, "—")
            except Exception:
                rn = "—"
            await safe(q.answer, f"⛔ Нельзя — его ранг: {rn}", show_alert=True)
            return

    settings = await get_settings(chat_id)
    warn_limit = int(_s(settings, "warn_limit", 3))
    auto_mute_sec = int(_s(settings, "auto_mute_sec", 3600))
    default_mute_sec = int(_s(settings, "default_mute_sec", 600))

    try:
        member = await ctx.bot.get_chat_member(chat_id, target_id)
        mention = member.user.mention_html()
        target_user_obj = member.user
    except Exception:
        mention = f"<code>{target_id}</code>"
        target_user_obj = None

    result = ""

    if action == "warn":
        total = await add_warn(target_id, chat_id)
        await log_action(ctx.bot, q.from_user, f"⚠️ Варн ({total}/{warn_limit}) [кнопка]",
                         target_user_obj or target_id, "", chat_id)
        result = f"⚠️ {mention} — варн ({total}/{warn_limit})"
        if total >= warn_limit:
            warn_action = str(_s(settings, "warn_action", "ban")).lower()
            try:
                if warn_action == "ban":
                    await ctx.bot.ban_chat_member(chat_id, target_id)
                    await add_blacklist(target_id, chat_id, "auto_ban_warns")
                    await reset_warns(target_id, chat_id)
                    await log_action(ctx.bot, q.from_user,
                                     f"🔨 Авто-бан ({warn_limit}/{warn_limit} варнов) [кнопка]",
                                     target_user_obj or target_id, "", chat_id)
                    result += " → 🔨 авто-бан"
                elif warn_action == "kick":
                    await ctx.bot.ban_chat_member(chat_id, target_id)
                    await ctx.bot.unban_chat_member(chat_id, target_id)
                    await reset_warns(target_id, chat_id)
                    await log_action(ctx.bot, q.from_user,
                                     f"👢 Авто-кик ({warn_limit}/{warn_limit} варнов) [кнопка]",
                                     target_user_obj or target_id, "", chat_id)
                    result += " → 👢 авто-кик"
                else:
                    ok = await _mute_user(ctx.bot, chat_id, target_id, auto_mute_sec)
                    if ok:
                        await reset_warns(target_id, chat_id)
                        await log_action(ctx.bot, q.from_user,
                                         f"🔇 Авто-мут {fmt_seconds(auto_mute_sec)} ({warn_limit}/{warn_limit}) [кнопка]",
                                         target_user_obj or target_id, "", chat_id)
                        result += f" → 🔇 авто-мут {fmt_seconds(auto_mute_sec)}"
            except Exception as e:
                result += f" (ошибка: {esc(e)})"

    elif action == "mute":
        ok = await _mute_user(ctx.bot, chat_id, target_id, default_mute_sec)
        if ok:
            await log_action(ctx.bot, q.from_user, f"🔇 Мут {fmt_seconds(default_mute_sec)} [кнопка]",
                             target_user_obj or target_id, "", chat_id)
            result = f"🔇 {mention} — мут {fmt_seconds(default_mute_sec)}"
        else:
            result = "❌ мут не удался"

    elif action == "kick":
        try:
            await ctx.bot.ban_chat_member(chat_id, target_id)
            await ctx.bot.unban_chat_member(chat_id, target_id)
            await log_action(ctx.bot, q.from_user, "👢 Кик [кнопка]",
                             target_user_obj or target_id, "", chat_id)
            result = f"👢 {mention} — кикнут"
        except Exception as e:
            result = f"❌ кик: {esc(e)}"

    elif action == "ban":
        try:
            await ctx.bot.ban_chat_member(chat_id, target_id)
            await add_blacklist(target_id, chat_id, "banned_via_button")
            await log_action(ctx.bot, q.from_user, "🔨 Бан [кнопка]",
                             target_user_obj or target_id, "", chat_id)
            result = f"🔨 {mention} — забанен"
        except Exception as e:
            result = f"❌ бан: {esc(e)}"

    elif action == "unban":
        try:
            await ctx.bot.unban_chat_member(chat_id, target_id)
            await remove_blacklist(target_id, chat_id)
            await log_action(ctx.bot, q.from_user, "🔓 Разбан [кнопка]",
                             target_user_obj or target_id, "", chat_id)
            result = f"🔓 {mention} — разбанен"
        except Exception as e:
            result = f"❌ разбан: {esc(e)}"

    elif action == "unmute":
        try:
            await ctx.bot.restrict_chat_member(chat_id, target_id, UNMUTE_PERMS)
            await set_mute(target_id, chat_id, None)
            await log_action(ctx.bot, q.from_user, "🔊 Размут [кнопка]",
                             target_user_obj or target_id, "", chat_id)
            result = f"🔊 {mention} — размучен"
        except Exception as e:
            result = f"❌ размут: {esc(e)}"

    elif action == "unwarn":
        await reset_warns(target_id, chat_id)
        await log_action(ctx.bot, q.from_user, "♻️ Анварн [кнопка]",
                         target_user_obj or target_id, "", chat_id)
        result = f"♻️ {mention} — варны сброшены"

    elif action == "profile":
        row = await get_user(target_id, chat_id)
        warns = row["warns"] if row else 0
        rank_val = await resolve_rank(ctx.bot, chat_id, target_id)
        circles = await circles_of_user(chat_id, target_id)
        circle_names = ", ".join(c["name"] for c in circles) if circles else "—"
        target_chat = q.message.chat.id
        target_thread = q.message.message_thread_id or 0
        send_kwargs = {}
        if target_thread:
            send_kwargs["message_thread_id"] = target_thread
        note = await safe(
            ctx.bot.send_message, target_chat,
            f"👤 <b>Профиль</b>\n"
            f"{mention}\n"
            f"ID: <code>{target_id}</code>\n"
            f"Ранг: {RANK_NAMES.get(rank_val, '—')}\n"
            f"⚠ Варны: {warns}/{warn_limit}\n"
            f"👥 Кружки: {circle_names}",
            parse_mode=ParseMode.HTML,
            **send_kwargs,
        )
        if note is not None:
            await _delayed(ctx.bot, target_chat, note.message_id)
        await safe(q.answer, "Профиль отправлен")
        return

    elif action == "close":
        if report_id:
            await close_report(report_id, "closed")
        result = "✅ Репорт закрыт"

    try:
        if action in ("ban", "kick", "unban") or action == "close":
            await safe(q.message.edit_reply_markup, reply_markup=None)
    except Exception:
        pass

    note = await safe(q.message.reply_text, result, parse_mode=ParseMode.HTML)
    if note is not None:
        await _delayed(ctx.bot, note.chat.id, note.message_id)
    await safe(q.answer, "Готово")


@mod_action
async def cmd_trigger(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message; user = update.effective_user
    if msg is None or user is None or msg.chat.type == "private":
        return
    my_rank = await resolve_rank(ctx.bot, msg.chat.id, user.id)
    if my_rank < RANK_SENIOR_MOD:
        await _rank_error(update, RANK_SENIOR_MOD, my_rank); return

    parts = (msg.text or "").split()
    if len(parts) < 2:
        await eph(
            msg,
            "<b>Управление триггерами</b>\n"
            "<code>триггер добавить слово варн</code>\n"
            "<code>триггер удалить слово</code>\n"
            "<code>триггер список</code>\n\n"
            f"Действия: <b>{ACTION_HINTS}</b>",
            parse_mode=ParseMode.HTML,
        )
        return

    sub = parts[1].lower()

    if sub in ("add", "добавить", "доб") and len(parts) >= 3:
        word = parts[2].lower()
        raw_action = parts[3].lower() if len(parts) > 3 else "варн"
        action = ACTION_ALIASES.get(raw_action)
        if not action:
            await eph(msg, f"❌ Неизвестное действие <code>{esc(raw_action)}</code>.\n\n"
                          f"Доступно: <b>{ACTION_HINTS}</b>",
                      parse_mode=ParseMode.HTML); return
        await add_trigger(msg.chat.id, word, action)
        await log_action(ctx.bot, user, f"📌 Триггер +{word} → {action}", "—", "", msg.chat.id)
        await eph(msg, f"✅ Триггер <code>{esc(word)}</code> → <b>{raw_action}</b>",
                  parse_mode=ParseMode.HTML)

    elif sub in ("del", "delete", "удалить", "уд") and len(parts) >= 3:
        w = parts[2].lower()
        await del_trigger(msg.chat.id, w)
        await log_action(ctx.bot, user, f"📌 Триггер −{w}", "—", "", msg.chat.id)
        await eph(msg, "🗑 Триггер удалён.")

    elif sub in ("list", "список", "спис"):
        rows = await list_triggers(msg.chat.id)
        if not rows:
            await eph(msg, "Триггеров нет."); return
        txt = "\n".join(f"• <code>{esc(r['word'])}</code> → {r['action']}" for r in rows)
        await eph(msg, f"<b>Триггеры:</b>\n{txt}", parse_mode=ParseMode.HTML)
    else:
        await eph(msg,
                  "❌ Неизвестная подкоманда.\n\n"
                  "Доступно:\n"
                  "• <code>триггер добавить слово действие</code>\n"
                  "• <code>триггер удалить слово</code>\n"
                  "• <code>триггер список</code>",
                  parse_mode=ParseMode.HTML)


@mod_action
async def cmd_create_circle(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message; user = update.effective_user
    if msg is None or user is None or msg.chat.type == "private":
        return
    my_rank = await resolve_rank(ctx.bot, msg.chat.id, user.id)
    if my_rank < RANK_JUNIOR_MOD:
        await _rank_error(update, RANK_JUNIOR_MOD, my_rank); return

    parts = (msg.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await eph(msg, "❌ Использование: <code>создатькружок Название</code>",
                  parse_mode=ParseMode.HTML)
        return

    name = parts[1].strip()[:60]
    thread_id = msg.message_thread_id or 0

    existing = await get_circle_by_thread(msg.chat.id, thread_id)
    if existing:
        await eph(msg, f"В этой теме уже есть кружок: <b>{esc(existing['name'])}</b>",
                  parse_mode=ParseMode.HTML)
        return

    cid = await create_circle(msg.chat.id, name, user.id, thread_id)
    await add_circle_member(cid, user.id)
    await eph(msg,
              f"✅ Кружок <b>{esc(name)}</b> создан.\n"
              f"Вступить: <code>вступить {esc(name)}</code>",
              parse_mode=ParseMode.HTML)


@mod_action
async def cmd_join(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message; user = update.effective_user
    if msg is None or user is None or msg.chat.type == "private":
        return
    parts = (msg.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await eph(msg, "❌ Использование: <code>вступить Название</code>",
                  parse_mode=ParseMode.HTML); return
    c = await get_circle_by_name(msg.chat.id, parts[1].strip())
    if not c:
        await eph(msg, "❌ Кружок не найден."); return
    await add_circle_member(c["id"], user.id)
    await eph(msg, f"✅ Ты в кружке <b>{esc(c['name'])}</b>.", parse_mode=ParseMode.HTML)


@mod_action
async def cmd_leave(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message; user = update.effective_user
    if msg is None or user is None or msg.chat.type == "private":
        return
    parts = (msg.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await eph(msg, "❌ Использование: <code>выйти Название</code>",
                  parse_mode=ParseMode.HTML); return
    c = await get_circle_by_name(msg.chat.id, parts[1].strip())
    if not c:
        await eph(msg, "❌ Кружок не найден."); return
    await remove_circle_member(c["id"], user.id)
    await eph(msg, f"🚪 Ты вышел из <b>{esc(c['name'])}</b>.", parse_mode=ParseMode.HTML)


@mod_action
async def cmd_circle_info(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if msg is None or msg.chat.type == "private":
        return
    parts = (msg.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await eph(msg, "❌ Использование: <code>инфокружка Название</code>",
                  parse_mode=ParseMode.HTML); return
    c = await get_circle_by_name(msg.chat.id, parts[1].strip())
    if not c:
        await eph(msg, "❌ Кружок не найден."); return
    members = await circle_members(c["id"])
    lines = [f"👥 <b>{esc(c['name'])}</b> — {len(members)} участн."]
    for row in members[:50]:
        uid = row["user_id"]
        try:
            m = await ctx.bot.get_chat_member(msg.chat.id, uid)
            lines.append(f"• {m.user.mention_html()}")
        except Exception:
            lines.append(f"• <code>{uid}</code>")
    await eph(msg, "\n".join(lines), parse_mode=ParseMode.HTML)


@mod_action
async def cmd_delete_circle(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message; user = update.effective_user
    if msg is None or user is None or msg.chat.type == "private":
        return
    my_rank = await resolve_rank(ctx.bot, msg.chat.id, user.id)
    if my_rank < RANK_JUNIOR_MOD:
        await _rank_error(update, RANK_JUNIOR_MOD, my_rank); return
    parts = (msg.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await eph(msg, "❌ Использование: <code>удалитькружок Название</code>",
                  parse_mode=ParseMode.HTML); return
    c = await get_circle_by_name(msg.chat.id, parts[1].strip())
    if not c:
        await eph(msg, "❌ Кружок не найден."); return
    if c["owner_id"] != user.id and my_rank < RANK_SENIOR_ADMIN:
        await eph(msg, "❌ Только владелец кружка или старший админ."); return
    await delete_circle(c["id"])
    await eph(msg, f"🗑 Кружок <b>{esc(c['name'])}</b> удалён.", parse_mode=ParseMode.HTML)


@mod_action
async def cmd_clean(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message; user = update.effective_user
    if msg is None or user is None or msg.chat.type == "private":
        return
    my_rank = await resolve_rank(ctx.bot, msg.chat.id, user.id)
    if my_rank < RANK_JUNIOR_MOD:
        await _rank_error(update, RANK_JUNIOR_MOD, my_rank); return

    thread_id = msg.message_thread_id or 0
    parts = (msg.text or "").split()
    try:
        n = int(parts[1]) if len(parts) > 1 else 30
    except ValueError:
        n = 30
    n = max(1, min(n, 100))

    rows = await last_messages(msg.chat.id, thread_id, n)
    ids = [r["message_id"] for r in rows]
    deleted = 0
    for mid in ids:
        try:
            await ctx.bot.delete_message(msg.chat.id, mid)
            deleted += 1
        except Exception:
            pass
    await forget_messages(msg.chat.id, ids)
    await log_action(ctx.bot, user, f"🧹 Чистка: {deleted} сообщ.", "—", "", msg.chat.id)
    note_kwargs = {"message_thread_id": thread_id} if thread_id else {}
    note = await safe(
        ctx.bot.send_message, msg.chat.id,
        f"🧹 Удалено: {deleted}",
        **note_kwargs,
    )
    if note is not None:
        await _delayed(ctx.bot, msg.chat.id, note.message_id)


@mod_action
async def cmd_links(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message; user = update.effective_user
    if msg is None or user is None or msg.chat.type == "private":
        return
    my_rank = await resolve_rank(ctx.bot, msg.chat.id, user.id)
    if my_rank < RANK_SENIOR_MOD:
        await _rank_error(update, RANK_SENIOR_MOD, my_rank); return

    parts = (msg.text or "").split()
    arg = parts[1].lower() if len(parts) > 1 else ""
    if arg not in ("on", "off", "вкл", "выкл", "включить", "выключить"):
        await eph(msg, "❌ Использование: <code>ссылки вкл</code> или <code>ссылки выкл</code>",
                  parse_mode=ParseMode.HTML); return

    thread_id = msg.message_thread_id or 0
    allowed = arg in ("on", "вкл", "включить")
    await set_topic_links(msg.chat.id, thread_id, allowed)
    await log_action(ctx.bot, user, f"🔗 Ссылки: {'вкл' if allowed else 'выкл'}", "—", "", msg.chat.id)
    await eph(msg,
              f"🔗 Ссылки в этой теме: <b>{'разрешены' if allowed else 'запрещены'}</b>",
              parse_mode=ParseMode.HTML)


@mod_action
async def cmd_report(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message; user = update.effective_user
    if msg is None or user is None or msg.chat.type == "private":
        return
    if msg.reply_to_message is None or msg.reply_to_message.from_user is None:
        await eph(msg, "❌ Ответь на сообщение нарушителя и напиши <code>репорт причина</code>",
                  parse_mode=ParseMode.HTML); return

    target = msg.reply_to_message.from_user
    if target.id == user.id or target.is_bot:
        await eph(msg, "❌ Нельзя репортить себя или бота."); return

    parts = (msg.text or "").split(maxsplit=1)
    reason = parts[1].strip() if len(parts) > 1 else "—"

    chat_id_str = str(msg.chat.id)
    if chat_id_str.startswith("-100"):
        link = f"https://t.me/c/{chat_id_str[4:]}/{msg.reply_to_message.message_id}"
    else:
        link = ""

    rid = await create_report(
        msg.chat.id, msg.message_thread_id or 0,
        user.id, target.id, reason, link,
    )

    text = (
        f"🚨 <b>Репорт #{rid}</b>\n"
        f"Чат: <code>{msg.chat.id}</code>\n"
        f"Тема: <code>{msg.message_thread_id or 0}</code>\n"
        f"Нарушитель: {target.mention_html()} (<code>{target.id}</code>)\n"
        f"От: {user.mention_html()} (<code>{user.id}</code>)\n"
        f"Причина: {esc(reason)}"
    )
    if link:
        text += f"\n<a href='{link}'>→ Сообщение</a>"

    kb = mod_kb(msg.chat.id, target.id, report_id=rid)
    sent = await send_admin(
        ctx.bot, text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=kb,
    )
    if sent is None:
        await eph(msg, "⚠️ Не смог отправить, попробуй позже."); return
    await eph(msg, "✅ Жалоба отправлена модераторам.",
              disable_notification=True)


async def _build_profile(ctx, chat_id: int, target) -> str:
    row = await get_user(target.id, chat_id)
    warns = row["warns"] if row else 0
    rank_val = await resolve_rank(ctx.bot, chat_id, target.id)
    circles = await circles_of_user(chat_id, target.id)
    circle_names = ", ".join(c["name"] for c in circles) if circles else "—"
    settings = await get_settings(chat_id)
    warn_limit = int(_s(settings, "warn_limit", 3))
    lines = [
        "👤 <b>Профиль</b>",
        f"Имя: {target.mention_html()}",
        f"ID: <code>{target.id}</code>",
        f"Ранг: <b>{RANK_NAMES.get(rank_val, '—')}</b>",
        f"⚠ Предупреждений: <b>{warns}/{warn_limit}</b>",
        f"👥 Кружки: {circle_names}",
    ]
    if row and row["mute_until"]:
        lines.append(f"🔇 Мут до: <code>{esc(row['mute_until'])}</code>")
    return "\n".join(lines)


@mod_action
async def cmd_profile(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message; user = update.effective_user
    if msg is None or user is None:
        return
    if msg.reply_to_message and msg.reply_to_message.from_user:
        target = msg.reply_to_message.from_user
    else:
        parts = (msg.text or "").split()
        target = None
        if len(parts) > 1 and parts[1].lstrip("-").isdigit():
            try:
                m = await ctx.bot.get_chat_member(msg.chat.id, int(parts[1]))
                target = m.user
            except Exception:
                target = None
        if target is None:
            target = user
    await eph(msg,
              await _build_profile(ctx, msg.chat.id, target),
              parse_mode=ParseMode.HTML)


@mod_action
async def cmd_me(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message; user = update.effective_user
    if msg is None or user is None:
        return
    await eph(msg,
              await _build_profile(ctx, msg.chat.id, user),
              parse_mode=ParseMode.HTML)


SETTING_ALIASES = {
    "antispam":     "antispam",
    "антиспам":     "antispam",
    "antiraid":     "antiraid",
    "антирейд":     "antiraid",
    "triggers":     "triggers_on",
    "триггеры":     "triggers_on",
    "floodmute":    "flood_mute_sec",
    "мутфлуд":      "flood_mute_sec",
    "warnlimit":    "warn_limit",
    "варнлимит":    "warn_limit",
    "automute":     "auto_mute_sec",
    "автомут":      "auto_mute_sec",
    "triggermute":  "trig_mute_sec",
    "триггермут":   "trig_mute_sec",
    "defmute":      "default_mute_sec",
    "дефмут":       "default_mute_sec",
    "warnaction":   "warn_action",
    "варндействие": "warn_action",
    "варнэкшн":     "warn_action",
    "arjoins":      "antiraid_joins",
    "рейдвходы":    "antiraid_joins",
    "arwindow":     "antiraid_window",
    "рейдокно":     "antiraid_window",
    "arlookback":   "antiraid_lookback",
    "рейдкулдаун":  "antiraid_lookback",
    "ephemeral":    "ephemeral_delay",
    "удаление":     "ephemeral_delay",
    "trigwarn":     "trig_warn_limit",
    "срварн":       "trig_warn_limit",
    "trigwarnlimit": "trig_warn_limit",
    "links":        "links_forbidden",
    "ссылки":       "links_forbidden",
}

BOOL_SETTINGS = {"antispam", "antiraid", "triggers_on", "links_forbidden"}

TIME_SETTINGS = {"flood_mute_sec", "auto_mute_sec", "trig_mute_sec", "default_mute_sec",
                 "antiraid_window", "antiraid_lookback", "ephemeral_delay"}

INT_SETTINGS = {"warn_limit", "antiraid_joins", "trig_warn_limit"}

CHOICE_SETTINGS = {
    "warn_action": ("mute", "ban", "kick"),
}


def _settings_help() -> str:
    return (
        "<b>Управление настройками</b>\n\n"
        "<b>Время пишется так:</b> <code>10с</code> · <code>30м</code> · <code>1ч</code> · <code>2д</code>\n"
        "(с=секунды, м=минуты, ч=часы, д=дни).\n\n"
        "<b>Тумблеры (вкл/выкл):</b>\n"
        "• <code>настройки антиспам вкл|выкл</code>\n"
        "• <code>настройки антирейд вкл|выкл</code>\n"
        "• <code>настройки триггеры вкл|выкл</code>\n"
        "• <code>настройки ссылки вкл|выкл</code> — глобальный запрет ссылок в чате\n\n"
        "<b>Числа:</b>\n"
        "• <code>настройки варнлимит N</code> — варнов до авто-наказания (команда)\n"
        "• <code>настройки срварн N</code> — отдельный лимит варнов от триггеров\n"
        "• <code>настройки рейдвходы N</code> — сколько входов за окно → алерт\n"
        "• <code>настройки флуд N 10с</code> — N сообщений за время\n\n"
        "<b>Время:</b>\n"
        "• <code>настройки мутфлуд 30м</code> — мут за флуд\n"
        "• <code>настройки автомут 1ч</code> — длительность авто-мута\n"
        "• <code>настройки триггермут 1ч</code> — мут по триггеру\n"
        "• <code>настройки дефмут 10м</code> — мут без времени\n"
        "• <code>настройки рейдокно 30с</code> — окно вступлений для антирейда\n"
        "• <code>настройки рейдкулдаун 30м</code> — кулдаун алертов\n"
        "• <code>настройки удаление 60с</code> — через сколько бот удаляет свои сообщения (0 = не удалять)\n\n"
        "<b>Действие при N варнах:</b>\n"
        "• <code>настройки варндействие mute</code> — мут\n"
        "• <code>настройки варндействие ban</code> — бан (по умолчанию)\n"
        "• <code>настройки варндействие kick</code> — кик\n"
    )


@mod_action
async def cmd_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message; user = update.effective_user
    if msg is None or user is None or msg.chat.type == "private":
        return
    my_rank = await resolve_rank(ctx.bot, msg.chat.id, user.id)
    if my_rank < RANK_SENIOR_ADMIN:
        await _rank_error(update, RANK_SENIOR_ADMIN, my_rank); return

    parts = (msg.text or "").split()

    if len(parts) == 1:
        s = await get_settings(msg.chat.id)
        cur = (
            f"<b>Текущие настройки</b>\n"
            f"🔹 Антиспам: <b>{'вкл' if s['antispam'] else 'выкл'}</b>\n"
            f"🔹 Антирейд: <b>{'вкл' if s['antiraid'] else 'выкл'}</b> "
            f"(<b>{_s(s, 'antiraid_joins', 10)}</b> входов / <b>{fmt_seconds(int(_s(s, 'antiraid_window', 30)))}</b>, "
            f"кулдаун <b>{fmt_seconds(int(_s(s, 'antiraid_lookback', 1800)))}</b>)\n"
            f"🔹 Триггеры: <b>{'вкл' if s['triggers_on'] else 'выкл'}</b>\n"
            f"🔹 Ссылки запрещены: <b>{'да' if _s(s, 'links_forbidden', 0) else 'нет'}</b>\n"
            f"🔹 Флуд: <b>{s['flood_limit']}</b> сообщ. / <b>{fmt_seconds(int(s['flood_window']))}</b>\n"
            f"🔹 Мут за флуд: <b>{fmt_seconds(int(_s(s, 'flood_mute_sec', 1800)))}</b>\n"
            f"🔹 Варнов до авто-наказания: <b>{_s(s, 'warn_limit', 3)}</b>\n"
            f"🔹 Варнов у триггеров: <b>{_s(s, 'trig_warn_limit', 3)}</b>\n"
            f"🔹 Действие при N варнах: <b>{_s(s, 'warn_action', 'ban')}</b>\n"
            f"🔹 Авто-мут: <b>{fmt_seconds(int(_s(s, 'auto_mute_sec', 3600)))}</b>\n"
            f"🔹 Мут по триггеру: <b>{fmt_seconds(int(_s(s, 'trig_mute_sec', 3600)))}</b>\n"
            f"🔹 Мут по умолчанию: <b>{fmt_seconds(int(_s(s, 'default_mute_sec', 600)))}</b>\n"
            f"🔹 Удаление своих сообщений: <b>{fmt_seconds(int(_s(s, 'ephemeral_delay', 60)))}</b>\n\n"
            + _settings_help()
        )
        await eph(msg, cur, parse_mode=ParseMode.HTML, delay=60.0)
        return

    key_raw = parts[1].lower()

    if key_raw in ("flood", "флуд"):
        if len(parts) < 4:
            await eph(msg, "❌ Использование: <code>настройки флуд N 10с</code>",
                      parse_mode=ParseMode.HTML); return
        try:
            n = int(parts[2])
        except ValueError:
            await eph(msg, "❌ Первое значение — число сообщений."); return
        w = parse_setting_time(parts[3])
        if n <= 0 or w <= 0:
            await eph(msg, "❌ Оба значения должны быть > 0."); return
        await set_setting(msg.chat.id, "flood_limit", n)
        await set_setting(msg.chat.id, "flood_window", w)
        await log_action(ctx.bot, user, f"⚙️ flood → {n} за {fmt_seconds(w)}", "—", "", msg.chat.id)
        await eph(msg,
                  f"✅ Флуд-лимит: <b>{n}</b> сообщ. за <b>{fmt_seconds(w)}</b>",
                  parse_mode=ParseMode.HTML)
        return

    if key_raw not in SETTING_ALIASES:
        await eph(msg,
                  f"❌ Параметр <code>{esc(key_raw)}</code> не найден.\n\n"
                  + _settings_help(),
                  parse_mode=ParseMode.HTML, delay=60.0)
        return

    key = SETTING_ALIASES[key_raw]

    if key in CHOICE_SETTINGS:
        variants = CHOICE_SETTINGS[key]
        if len(parts) < 3:
            await eph(msg, f"❌ Использование: <code>настройки {key_raw} {'|'.join(variants)}</code>",
                      parse_mode=ParseMode.HTML); return
        val = parts[2].lower()
        if val not in variants:
            await eph(msg, f"❌ Значение должно быть: <b>{' · '.join(variants)}</b>",
                      parse_mode=ParseMode.HTML); return
        await set_setting(msg.chat.id, key, val)
        await log_action(ctx.bot, user, f"⚙️ {key} → {val}", "—", "", msg.chat.id)
        await eph(msg, f"✅ {key_raw} → <b>{val}</b>", parse_mode=ParseMode.HTML)
        return

    if key in BOOL_SETTINGS:
        if len(parts) < 3:
            await eph(msg, f"❌ Использование: <code>настройки {key_raw} вкл|выкл</code>",
                      parse_mode=ParseMode.HTML); return
        raw = parts[2].lower()
        if raw in ("on", "1", "вкл", "включить", "да", "yes", "true"):
            val = 1
        elif raw in ("off", "0", "выкл", "выключить", "нет", "no", "false"):
            val = 0
        else:
            await eph(msg, "❌ Значение должно быть <b>вкл</b> или <b>выкл</b>."); return
        await set_setting(msg.chat.id, key, val)
        await log_action(ctx.bot, user, f"⚙️ {key} → {'вкл' if val else 'выкл'}", "—", "", msg.chat.id)
        await eph(msg, f"✅ {key_raw} → <b>{'вкл' if val else 'выкл'}</b>",
                  parse_mode=ParseMode.HTML)
        return

    if len(parts) < 3:
        if key in TIME_SETTINGS:
            await eph(msg, f"❌ Использование: <code>настройки {key_raw} 30м</code>\n"
                          f"Формат: 10с / 30м / 1ч / 2д",
                      parse_mode=ParseMode.HTML)
        else:
            await eph(msg, f"❌ Использование: <code>настройки {key_raw} N</code>",
                      parse_mode=ParseMode.HTML)
        return

    if key in TIME_SETTINGS:
        secs = parse_setting_time(parts[2])
        if secs <= 0 and parts[2].strip() != "0":
            await eph(msg, "❌ Не распознал время. Пример: <code>10с</code> · <code>30м</code> · <code>1ч</code> · <code>2д</code>",
                      parse_mode=ParseMode.HTML)
            return
        if parts[2].strip() == "0":
            secs = 0
        await set_setting(msg.chat.id, key, secs)
        await log_action(ctx.bot, user, f"⚙️ {key} → {fmt_seconds(secs) if secs else 'выкл'}", "—", "", msg.chat.id)
        await eph(msg, f"✅ {key_raw} → <b>{fmt_seconds(secs) if secs else 'выкл'}</b>",
                  parse_mode=ParseMode.HTML)
        return

    try:
        val = int(parts[2])
    except ValueError:
        await eph(msg, "❌ Нужно число."); return
    if val < 0:
        await eph(msg, "❌ Число не может быть отрицательным."); return

    await set_setting(msg.chat.id, key, val)
    await log_action(ctx.bot, user, f"⚙️ {key} → {val}", "—", "", msg.chat.id)
    await eph(msg, f"✅ {key_raw} → <b>{val}</b>", parse_mode=ParseMode.HTML)


async def _post_init(app: Application):
    try:
        await app.bot.set_my_commands([
            BotCommand("help", "Список команд"),
            BotCommand("mod", "Меню модерации (реплай)"),
            BotCommand("report", "Жалоба (реплай)"),
            BotCommand("me", "Мой профиль"),
            BotCommand("profile", "Профиль участника"),
            BotCommand("settings", "Настройки авто-действий"),
        ])
    except Exception as e:
        log.warning("set_my_commands failed: %s", e)


async def _error_handler(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    err = ctx.error
    if err is None:
        return
    name = type(err).__name__
    if name in ("TimedOut", "NetworkError", "ConnectTimeout", "ReadTimeout", "RetryAfter"):
        log.warning("Сеть глюкнула (%s) — продолжаю.", name)
        return
    log.exception("Unhandled error in handler:", exc_info=err)


RU_ALIASES = [
    (r"^бан(?:\s|$)",              cmd_ban),
    (r"^разбан(?:\s|$)",           cmd_unban),
    (r"^кик(?:\s|$)",              cmd_kick),
    (r"^мут(?:\s|$)",              cmd_mute),
    (r"^размут(?:\s|$)",           cmd_unmute),
    (r"^варн(?:\s|$)",             cmd_warn),
    (r"^анварн(?:\s|$)",           cmd_unwarn),
    (r"^варны(?:\s|$)",            cmd_warns),
    (r"^мод(?:\s|$)",              cmd_mod),
    (r"^репорт(?:\s|$)",           cmd_report),
    (r"^профиль(?:\s|$)",          cmd_profile),
    (r"^я$",                       cmd_me),
    (r"^помощь(?:\s|$)",           cmd_help),
    (r"^ранги(?:\s|$)",            cmd_ranks),
    (r"^роли(?:\s|$)",             cmd_roles),
    (r"^списокролей(?:\s|$)",      cmd_roles),
    (r"^всеранги(?:\s|$)",         cmd_roles),
    (r"^чистка(?:\s|$)",           cmd_clean),
    (r"^ссылки(?:\s|$)",           cmd_links),
    (r"^выдатьранг(?:\s|$)",       cmd_setrank),
    (r"^снятьранг(?:\s|$)",        cmd_unrank),
    (r"^создатькружок(?:\s|$)",    cmd_create_circle),
    (r"^вступить(?:\s|$)",         cmd_join),
    (r"^выйти(?:\s|$)",            cmd_leave),
    (r"^инфокружка(?:\s|$)",       cmd_circle_info),
    (r"^удалитькружок(?:\s|$)",    cmd_delete_circle),
    (r"^триггер(?:\s|$)",          cmd_trigger),
    (r"^настройки(?:\s|$)",        cmd_settings),
]


async def _build_app() -> Application:
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .connect_timeout(CONNECT_TIMEOUT)
        .read_timeout(READ_TIMEOUT)
        .write_timeout(WRITE_TIMEOUT)
        .pool_timeout(POOL_TIMEOUT)
        .get_updates_connect_timeout(CONNECT_TIMEOUT)
        .get_updates_read_timeout(READ_TIMEOUT)
        .post_init(_post_init)
        .build()
    )

    G = filters.ChatType.GROUPS | filters.ChatType.SUPERGROUP

    app.add_handler(
        MessageHandler(filters.ChatType.PRIVATE, private_block_mw),
        group=-100,
    )

    app.add_handler(MessageHandler(G, pre_remember), group=-4)

    app.add_handler(MessageHandler(G & ~filters.COMMAND, antispam_mw), group=-3)
    app.add_handler(MessageHandler(G & (filters.TEXT | filters.CAPTION), link_guard_mw), group=-2)
    app.add_handler(MessageHandler(G & (filters.TEXT | filters.CAPTION), trigger_mw), group=-1)

    app.add_handler(ChatMemberHandler(antiraid_track, chat_member_types=ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(ChatMemberHandler(on_bot_added, chat_member_types=ChatMemberHandler.CHAT_MEMBER))

    app.add_handler(CallbackQueryHandler(cb_mod, pattern=r"^mod:"))

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("ranks", cmd_ranks))
    app.add_handler(CommandHandler("roles", cmd_roles))
    app.add_handler(CommandHandler("setrank", cmd_setrank, filters=G))
    app.add_handler(CommandHandler("unrank", cmd_unrank, filters=G))

    app.add_handler(CommandHandler("ban", cmd_ban, filters=G))
    app.add_handler(CommandHandler("unban", cmd_unban, filters=G))
    app.add_handler(CommandHandler("kick", cmd_kick, filters=G))
    app.add_handler(CommandHandler("mute", cmd_mute, filters=G))
    app.add_handler(CommandHandler("unmute", cmd_unmute, filters=G))
    app.add_handler(CommandHandler("warn", cmd_warn, filters=G))
    app.add_handler(CommandHandler("unwarn", cmd_unwarn, filters=G))
    app.add_handler(CommandHandler("warns", cmd_warns, filters=G))
    app.add_handler(CommandHandler("mod", cmd_mod, filters=G))

    app.add_handler(CommandHandler("trigger", cmd_trigger, filters=G))

    app.add_handler(CommandHandler("create_circle", cmd_create_circle, filters=G))
    app.add_handler(CommandHandler("join", cmd_join, filters=G))
    app.add_handler(CommandHandler("leave", cmd_leave, filters=G))
    app.add_handler(CommandHandler("circle_info", cmd_circle_info, filters=G))
    app.add_handler(CommandHandler("delete_circle", cmd_delete_circle, filters=G))

    app.add_handler(CommandHandler("clean", cmd_clean, filters=G))
    app.add_handler(CommandHandler("links", cmd_links, filters=G))

    app.add_handler(CommandHandler("report", cmd_report, filters=G))

    app.add_handler(CommandHandler("profile", cmd_profile, filters=G))
    app.add_handler(CommandHandler("me", cmd_me, filters=G))

    app.add_handler(CommandHandler("settings", cmd_settings, filters=G))

    for pattern, handler in RU_ALIASES:
        app.add_handler(MessageHandler(
            G & filters.TEXT & filters.Regex(re.compile(pattern, re.IGNORECASE)),
            handler,
        ))

    app.add_error_handler(_error_handler)
    return app


async def _start_with_retry() -> Application:
    attempt = 0
    while True:
        attempt += 1
        app = await _build_app()
        try:
            log.info("Подключение к Telegram (попытка %d)…", attempt)
            await app.initialize()
            await app.start()
            await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
            log.info("Bot running. Ctrl+C to stop.")
            return app
        except (TimedOut, NetworkError) as e:
            log.warning("Сеть недоступна (%s). Жду %.0f сек…",
                        type(e).__name__, START_RETRY_DELAY)
            try:
                await app.shutdown()
            except Exception:
                pass
            await asyncio.sleep(START_RETRY_DELAY)
        except Exception as e:
            log.exception("Фатальная ошибка при старте: %s", e)
            raise


async def _run_bot():
    app = await _start_with_retry()
    stop_event = asyncio.Event()
    try:
        await stop_event.wait()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        log.info("Shutting down…")
        for step in (app.updater.stop, app.stop, app.shutdown):
            try:
                await step()
            except Exception:
                pass


def main():
    _sync_init_db()
    log.info("Bot starting…")
    try:
        asyncio.run(_run_bot())
    except (KeyboardInterrupt, SystemExit):
        log.info("Bot stopped.")


if __name__ == "__main__":
    main()
