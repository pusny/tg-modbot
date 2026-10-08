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


def _req_float(key: str, default: float) -> float:
    val = os.getenv(key)
    if val is None or val.strip() == "":
        return default
    return float(val.strip())


BOT_TOKEN        = _req("BOT_TOKEN")
ADMIN_CHAT_ID    = _req_int("ADMIN_CHAT_ID")
REPORT_THREAD_ID = _req_int("REPORT_THREAD_ID", 0) or None
DB_PATH          = os.getenv("DB_PATH", "bot.db")

ANTISPAM_LIMIT   = _req_int("ANTISPAM_LIMIT", 7)
ANTISPAM_WINDOW  = _req_float("ANTISPAM_WINDOW", 10.0)
ANTISPAM_MUTE_M  = _req_int("ANTISPAM_MUTE_M", 30)

ANTIRAID_JOINS    = _req_int("ANTIRAID_JOINS", 10)
ANTIRAID_WINDOW   = _req_float("ANTIRAID_WINDOW", 30.0)
ANTIRAID_LOOKBACK = _req_float("ANTIRAID_LOOKBACK", 1800.0)

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
    "owner":        RANK_OWNER,
    "senior_admin": RANK_SENIOR_ADMIN,
    "junior_admin": RANK_JUNIOR_ADMIN,
    "senior_mod":   RANK_SENIOR_MOD,
    "junior_mod":   RANK_JUNIOR_MOD,
    "user":         RANK_USER,
}

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


async def send_admin(bot, text: str, **kwargs):
    kw = dict(kwargs)
    if REPORT_THREAD_ID:
        kw["message_thread_id"] = REPORT_THREAD_ID
    sent = await safe(bot.send_message, ADMIN_CHAT_ID, text, **kw)
    if sent is None and REPORT_THREAD_ID:
        kw.pop("message_thread_id", None)
        sent = await safe(bot.send_message, ADMIN_CHAT_ID, text, **kw)
    return sent


def parse_duration(s: str) -> int:
    if not s:
        return 0
    s = s.strip().lower()
    unit = s[-1]
    try:
        n = int(s[:-1])
    except ValueError:
        return 0
    return n * {"s": 1, "m": 60, "h": 3600, "d": 86400}.get(unit, 0)


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
    chat_id        INTEGER PRIMARY KEY,
    antispam       INTEGER NOT NULL DEFAULT 1,
    antiraid       INTEGER NOT NULL DEFAULT 1,
    flood_limit    INTEGER NOT NULL DEFAULT 7,
    flood_window   INTEGER NOT NULL DEFAULT 10,
    mute_minutes   INTEGER NOT NULL DEFAULT 30
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


def _sync_init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript(SCHEMA)
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


async def set_setting(chat_id: int, key: str, value: int):
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


async def _rank_error(update: Update, min_rank: int, rank: int):
    msg = update.effective_message
    if msg is None:
        return
    await safe(
        msg.reply_text,
        f"⛔ Недостаточно прав.\n"
        f"Нужен: <b>{RANK_NAMES.get(min_rank, '—')}</b>\n"
        f"У тебя: <b>{RANK_NAMES.get(rank, '—')}</b>",
        parse_mode=ParseMode.HTML,
    )


def mod_kb(chat_id: int, user_id: int, report_id: Optional[int] = None) -> InlineKeyboardMarkup:
    tail = f":{report_id}" if report_id else ""
    rows = [
        [
            InlineKeyboardButton("⚠️ Warn",   callback_data=f"mod:warn:{chat_id}:{user_id}{tail}"),
            InlineKeyboardButton("🔇 Mute",   callback_data=f"mod:mute:{chat_id}:{user_id}{tail}"),
            InlineKeyboardButton("👢 Kick",   callback_data=f"mod:kick:{chat_id}:{user_id}{tail}"),
        ],
        [
            InlineKeyboardButton("🔨 Ban",    callback_data=f"mod:ban:{chat_id}:{user_id}{tail}"),
            InlineKeyboardButton("🔊 Unmute", callback_data=f"mod:unmute:{chat_id}:{user_id}{tail}"),
            InlineKeyboardButton("♻️ Unwarn", callback_data=f"mod:unwarn:{chat_id}:{user_id}{tail}"),
        ],
        [
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
        minutes = settings["mute_minutes"]
        try:
            await ctx.bot.restrict_chat_member(
                msg.chat.id, user.id, MUTE_PERMS,
                until_date=int(time.time()) + minutes * 60,
            )
        except Exception as e:
            log.debug("antispam restrict fail: %s", e)

        await set_mute(user.id, msg.chat.id, None)
        await safe(
            msg.reply_text,
            f"🔇 {user.mention_html()} получил мут за флуд на {minutes} мин.",
            parse_mode=ParseMode.HTML,
        )
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

    thread_id = msg.message_thread_id or 0
    allowed = await get_topic_links_allowed(msg.chat.id, thread_id)
    if allowed:
        return

    rank = await resolve_rank(ctx.bot, msg.chat.id, user.id)
    if rank >= RANK_SENIOR_MOD:
        return

    await safe(msg.delete)
    await safe(
        msg.reply_text,
        "🚫 Ссылки в этой теме запрещены.",
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

    triggers = await list_triggers(msg.chat.id)
    if not triggers:
        return

    rank = await resolve_rank(ctx.bot, msg.chat.id, user.id)
    if rank >= RANK_SENIOR_MOD:
        return

    for row in triggers:
        word = row["word"]
        action = row["action"]
        if word and word in text:
            await safe(msg.delete)
            await _apply_trigger(ctx.bot, msg.chat.id, user, action, word)
            raise ApplicationHandlerStop


async def _apply_trigger(bot, chat_id: int, user, action: str, word: str):
    uid = user.id
    mention = user.mention_html()
    if action == "warn":
        total = await add_warn(uid, chat_id)
        await safe(
            bot.send_message, chat_id,
            f"⚠️ {mention} — предупреждение (триггер: <code>{esc(word)}</code>). Всего: {total}/3",
            parse_mode=ParseMode.HTML,
        )
        if total >= 3:
            await _mute_member(bot, chat_id, uid, 60)
            await reset_warns(uid, chat_id)
            await safe(bot.send_message, chat_id,
                       f"🔇 {mention} — авто-мут на 1 час (3/3).",
                       parse_mode=ParseMode.HTML)
    elif action == "mute":
        await _mute_member(bot, chat_id, uid, 60)
    elif action == "kick":
        try:
            await bot.ban_chat_member(chat_id, uid)
            await bot.unban_chat_member(chat_id, uid)
        except Exception as e:
            log.debug("kick fail: %s", e)
    elif action == "ban":
        try:
            await bot.ban_chat_member(chat_id, uid)
        except Exception as e:
            log.debug("ban fail: %s", e)


async def _mute_member(bot, chat_id: int, user_id: int, minutes: int):
    try:
        await bot.restrict_chat_member(
            chat_id, user_id, MUTE_PERMS,
            until_date=int(time.time()) + minutes * 60,
        )
        await set_mute(user_id, chat_id, None)
    except Exception as e:
        log.debug("mute fail: %s", e)


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

    old_status = cm.old_chat_member.status
    new_status = cm.new_chat_member.status
    if old_status in ("left", "kicked") and new_status in ("member", "restricted"):
        now = time.monotonic()
        bucket = _raid_buckets[chat_id]
        bucket.append(now)
        while bucket and now - bucket[0] > ANTIRAID_WINDOW:
            bucket.popleft()

        if len(bucket) >= ANTIRAID_JOINS:
            last_alert = _raid_alerted.get(chat_id, 0)
            if now - last_alert < ANTIRAID_LOOKBACK:
                return
            _raid_alerted[chat_id] = now
            await send_admin(
                ctx.bot,
                f"🚨 <b>Возможный рейд</b> в чате <code>{chat_id}</code>\n"
                f"За <b>{int(ANTIRAID_WINDOW)}</b> сек вошло <b>{len(bucket)}</b> новых участников.",
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
        f"Список команд: <code>/help</code>",
        parse_mode=ParseMode.HTML,
    )


HELP_TEXT = (
    "🛠 <b>Модератор-бот</b>\n\n"
    "<b>Ранги:</b>\n"
    "• <code>owner</code> — создатель\n"
    "• <code>senior_admin</code> / <code>junior_admin</code> — админы\n"
    "• <code>senior_mod</code> / <code>junior_mod</code> — модераторы\n\n"
    "<b>Управление рангами:</b>\n"
    "• <code>/setrank &lt;alias&gt;</code> — реплай, только owner\n"
    "• <code>/ranks</code> — список алиасов\n\n"
    "<b>Модерация:</b>\n"
    "• <code>/mod</code> — реплай, меню с кнопками\n"
    "• <code>/ban [причина]</code> / <code>/unban &lt;id&gt;</code>\n"
    "• <code>/kick</code> / <code>/mute 10m</code> / <code>/unmute</code>\n"
    "• <code>/warn</code> / <code>/unwarn</code> / <code>/warns</code>\n\n"
    "<b>Триггеры:</b>\n"
    "• <code>/trigger add &lt;слово&gt; [warn|mute|kick|ban]</code>\n"
    "• <code>/trigger del &lt;слово&gt;</code>\n"
    "• <code>/trigger list</code>\n\n"
    "<b>Кружки:</b>\n"
    "• <code>/create_circle &lt;имя&gt;</code> — в топике, мод+\n"
    "• <code>/join &lt;имя&gt;</code> / <code>/leave &lt;имя&gt;</code>\n"
    "• <code>/circle_info &lt;имя&gt;</code>\n"
    "• <code>/delete_circle &lt;имя&gt;</code>\n\n"
    "<b>Темы / чат:</b>\n"
    "• <code>/clean &lt;N&gt;</code> — снести N сообщений в текущей теме (или в чате)\n"
    "• <code>/links on|off</code> — запрет ссылок в текущей теме (или в чате)\n\n"
    "<b>Прочее:</b>\n"
    "• <code>/report [причина]</code> — реплай → админ-чат\n"
    "• <code>/profile</code> / <code>/me</code>\n"
    "• <code>/settings</code> — настройки антиспама"
)


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if msg is None:
        return
    if msg.chat.type == "private":
        await safe(msg.reply_text, HELP_TEXT, parse_mode=ParseMode.HTML)
        return
    await safe(msg.reply_text, "🤖 Бот активен. /help — список команд.")


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if msg is None:
        return
    await safe(msg.reply_text, HELP_TEXT, parse_mode=ParseMode.HTML,
               disable_web_page_preview=True)


async def cmd_ranks(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if msg is None:
        return
    lines = ["<b>Доступные ранги:</b>"]
    for alias, val in RANK_ALIASES.items():
        lines.append(f"• <code>{alias}</code> → {RANK_NAMES[val]}")
    await safe(msg.reply_text, "\n".join(lines), parse_mode=ParseMode.HTML)


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
        await safe(msg.reply_text,
                   "Ответь на сообщение и напиши: <code>/setrank junior_mod</code>",
                   parse_mode=ParseMode.HTML)
        return

    parts = (msg.text or "").split()
    if len(parts) < 2 or parts[1] not in RANK_ALIASES:
        await safe(msg.reply_text,
                   "Укажи ранг: " + ", ".join(RANK_ALIASES.keys()),
                   parse_mode=ParseMode.HTML)
        return

    target = msg.reply_to_message.from_user
    rank = RANK_ALIASES[parts[1]]
    await set_rank(target.id, msg.chat.id, rank)
    await safe(
        msg.reply_text,
        f"✅ {target.mention_html()} → <b>{RANK_NAMES[rank]}</b>",
        parse_mode=ParseMode.HTML,
    )


async def _target_user(msg):
    if msg.reply_to_message and msg.reply_to_message.from_user:
        return msg.reply_to_message.from_user
    return None


async def cmd_ban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message; user = update.effective_user
    if msg is None or user is None or msg.chat.type == "private":
        return
    my_rank = await resolve_rank(ctx.bot, msg.chat.id, user.id)
    if my_rank < RANK_JUNIOR_ADMIN:
        await _rank_error(update, RANK_JUNIOR_ADMIN, my_rank); return
    t = await _target_user(msg)
    if not t:
        await safe(msg.reply_text, "Ответь на сообщение нарушителя."); return
    reason = " ".join((msg.text or "").split()[1:]) or "—"
    try:
        await ctx.bot.ban_chat_member(msg.chat.id, t.id)
        await add_blacklist(t.id, msg.chat.id, "banned")
        await safe(msg.reply_text,
                   f"🔨 {t.mention_html()} забанен.\nПричина: {esc(reason)}",
                   parse_mode=ParseMode.HTML)
    except Exception as e:
        await safe(msg.reply_text, f"❌ Ошибка: {esc(e)}")


async def cmd_unban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message; user = update.effective_user
    if msg is None or user is None or msg.chat.type == "private":
        return
    my_rank = await resolve_rank(ctx.bot, msg.chat.id, user.id)
    if my_rank < RANK_JUNIOR_ADMIN:
        await _rank_error(update, RANK_JUNIOR_ADMIN, my_rank); return
    parts = (msg.text or "").split()
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
        await safe(msg.reply_text, "Использование: /unban &lt;user_id&gt;",
                   parse_mode=ParseMode.HTML); return
    uid = int(parts[1])
    try:
        await ctx.bot.unban_chat_member(msg.chat.id, uid)
        await remove_blacklist(uid, msg.chat.id)
        await safe(msg.reply_text, f"✅ Разбанен <code>{uid}</code>",
                   parse_mode=ParseMode.HTML)
    except Exception as e:
        await safe(msg.reply_text, f"❌ Ошибка: {esc(e)}")


async def cmd_kick(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message; user = update.effective_user
    if msg is None or user is None or msg.chat.type == "private":
        return
    my_rank = await resolve_rank(ctx.bot, msg.chat.id, user.id)
    if my_rank < RANK_JUNIOR_MOD:
        await _rank_error(update, RANK_JUNIOR_MOD, my_rank); return
    t = await _target_user(msg)
    if not t:
        await safe(msg.reply_text, "Ответь на сообщение."); return
    try:
        await ctx.bot.ban_chat_member(msg.chat.id, t.id)
        await ctx.bot.unban_chat_member(msg.chat.id, t.id)
        await safe(msg.reply_text, f"👢 {t.mention_html()} кикнут.",
                   parse_mode=ParseMode.HTML)
    except Exception as e:
        await safe(msg.reply_text, f"❌ Ошибка: {esc(e)}")


async def cmd_mute(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message; user = update.effective_user
    if msg is None or user is None or msg.chat.type == "private":
        return
    my_rank = await resolve_rank(ctx.bot, msg.chat.id, user.id)
    if my_rank < RANK_JUNIOR_MOD:
        await _rank_error(update, RANK_JUNIOR_MOD, my_rank); return
    t = await _target_user(msg)
    if not t:
        await safe(msg.reply_text, "Ответь на сообщение."); return
    parts = (msg.text or "").split()
    secs = parse_duration(parts[1]) if len(parts) > 1 else 600
    if secs <= 0:
        secs = 600
    try:
        await ctx.bot.restrict_chat_member(
            msg.chat.id, t.id, MUTE_PERMS,
            until_date=int(time.time()) + secs,
        )
        await set_mute(t.id, msg.chat.id, None)
        await safe(msg.reply_text,
                   f"🔇 {t.mention_html()} замучен на {fmt_seconds(secs)}.",
                   parse_mode=ParseMode.HTML)
    except Exception as e:
        await safe(msg.reply_text, f"❌ Ошибка: {esc(e)}")


async def cmd_unmute(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message; user = update.effective_user
    if msg is None or user is None or msg.chat.type == "private":
        return
    my_rank = await resolve_rank(ctx.bot, msg.chat.id, user.id)
    if my_rank < RANK_JUNIOR_MOD:
        await _rank_error(update, RANK_JUNIOR_MOD, my_rank); return
    t = await _target_user(msg)
    if not t:
        await safe(msg.reply_text, "Ответь на сообщение."); return
    try:
        await ctx.bot.restrict_chat_member(msg.chat.id, t.id, UNMUTE_PERMS)
        await set_mute(t.id, msg.chat.id, None)
        await safe(msg.reply_text, f"🔊 {t.mention_html()} размучен.",
                   parse_mode=ParseMode.HTML)
    except Exception as e:
        await safe(msg.reply_text, f"❌ Ошибка: {esc(e)}")


async def cmd_warn(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message; user = update.effective_user
    if msg is None or user is None or msg.chat.type == "private":
        return
    my_rank = await resolve_rank(ctx.bot, msg.chat.id, user.id)
    if my_rank < RANK_JUNIOR_MOD:
        await _rank_error(update, RANK_JUNIOR_MOD, my_rank); return
    t = await _target_user(msg)
    if not t:
        await safe(msg.reply_text, "Ответь на сообщение."); return
    total = await add_warn(t.id, msg.chat.id)
    await safe(msg.reply_text,
               f"⚠️ {t.mention_html()} — предупреждение ({total}/3).",
               parse_mode=ParseMode.HTML)
    if total >= 3:
        try:
            await ctx.bot.restrict_chat_member(
                msg.chat.id, t.id, MUTE_PERMS,
                until_date=int(time.time()) + 3600,
            )
            await reset_warns(t.id, msg.chat.id)
            await safe(msg.reply_text,
                       f"🔇 {t.mention_html()} — авто-мут на 1 час (3/3).",
                       parse_mode=ParseMode.HTML)
        except Exception as e:
            log.debug("auto-mute fail: %s", e)


async def cmd_unwarn(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message; user = update.effective_user
    if msg is None or user is None or msg.chat.type == "private":
        return
    my_rank = await resolve_rank(ctx.bot, msg.chat.id, user.id)
    if my_rank < RANK_JUNIOR_MOD:
        await _rank_error(update, RANK_JUNIOR_MOD, my_rank); return
    t = await _target_user(msg)
    if not t:
        await safe(msg.reply_text, "Ответь на сообщение."); return
    await reset_warns(t.id, msg.chat.id)
    await safe(msg.reply_text, f"♻️ Предупреждения {t.mention_html()} сброшены.",
               parse_mode=ParseMode.HTML)


async def cmd_warns(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message; user = update.effective_user
    if msg is None or user is None:
        return
    t = await _target_user(msg) or user
    row = await get_user(t.id, msg.chat.id)
    warns = row["warns"] if row else 0
    await safe(msg.reply_text,
               f"⚠️ {t.mention_html()}: {warns}/3 предупреждений.",
               parse_mode=ParseMode.HTML)


async def cmd_mod(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message; user = update.effective_user
    if msg is None or user is None or msg.chat.type == "private":
        return
    my_rank = await resolve_rank(ctx.bot, msg.chat.id, user.id)
    if my_rank < RANK_JUNIOR_MOD:
        await _rank_error(update, RANK_JUNIOR_MOD, my_rank); return
    t = await _target_user(msg)
    if not t:
        await safe(msg.reply_text, "Ответь на сообщение и напиши /mod"); return
    await safe(
        msg.reply_text,
        f"🛡 <b>Модерация</b>\n"
        f"Цель: {t.mention_html()} (<code>{t.id}</code>)",
        parse_mode=ParseMode.HTML,
        reply_markup=mod_kb(msg.chat.id, t.id),
    )


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
    rank_target = await resolve_rank(ctx.bot, chat_id, q.from_user.id)
    my_rank = max(rank_here, rank_target)
    if my_rank < required:
        await safe(q.answer,
                   f"⛔ Нужен {RANK_NAMES.get(required)}", show_alert=True)
        return

    try:
        member = await ctx.bot.get_chat_member(chat_id, target_id)
        mention = member.user.mention_html()
    except Exception:
        mention = f"<code>{target_id}</code>"

    result = ""

    if action == "warn":
        total = await add_warn(target_id, chat_id)
        result = f"⚠️ {mention} — варн ({total}/3)"
        if total >= 3:
            try:
                await ctx.bot.restrict_chat_member(
                    chat_id, target_id, MUTE_PERMS,
                    until_date=int(time.time()) + 3600,
                )
                await reset_warns(target_id, chat_id)
                result += " → 🔇 авто-мут 1ч"
            except Exception as e:
                result += f" (mute fail: {esc(e)})"

    elif action == "mute":
        try:
            await ctx.bot.restrict_chat_member(
                chat_id, target_id, MUTE_PERMS,
                until_date=int(time.time()) + 1800,
            )
            await set_mute(target_id, chat_id, None)
            result = f"🔇 {mention} — мут 30 мин"
        except Exception as e:
            result = f"❌ mute: {esc(e)}"

    elif action == "kick":
        try:
            await ctx.bot.ban_chat_member(chat_id, target_id)
            await ctx.bot.unban_chat_member(chat_id, target_id)
            result = f"👢 {mention} — кикнут"
        except Exception as e:
            result = f"❌ kick: {esc(e)}"

    elif action == "ban":
        try:
            await ctx.bot.ban_chat_member(chat_id, target_id)
            await add_blacklist(target_id, chat_id, "banned_via_button")
            result = f"🔨 {mention} — забанен"
        except Exception as e:
            result = f"❌ ban: {esc(e)}"

    elif action == "unmute":
        try:
            await ctx.bot.restrict_chat_member(chat_id, target_id, UNMUTE_PERMS)
            await set_mute(target_id, chat_id, None)
            result = f"🔊 {mention} — размучен"
        except Exception as e:
            result = f"❌ unmute: {esc(e)}"

    elif action == "unwarn":
        await reset_warns(target_id, chat_id)
        result = f"♻️ {mention} — варны сброшены"

    elif action == "profile":
        row = await get_user(target_id, chat_id)
        warns = row["warns"] if row else 0
        rank_val = await resolve_rank(ctx.bot, chat_id, target_id)
        circles = await circles_of_user(chat_id, target_id)
        circle_names = ", ".join(c["name"] for c in circles) if circles else "—"
        await safe(
            q.message.reply_text,
            f"👤 <b>Профиль</b>\n"
            f"{mention}\n"
            f"ID: <code>{target_id}</code>\n"
            f"Ранг: {RANK_NAMES.get(rank_val, '—')}\n"
            f"⚠ Варны: {warns}/3\n"
            f"👥 Кружки: {circle_names}",
            parse_mode=ParseMode.HTML,
        )
        await safe(q.answer, "Профиль отправлен")
        return

    elif action == "close":
        if report_id:
            await close_report(report_id, "closed")
        result = "✅ Репорт закрыт"

    try:
        if action in ("ban", "kick") or action == "close":
            await safe(q.message.edit_reply_markup, reply_markup=None)
    except Exception:
        pass

    await safe(q.message.reply_text, result, parse_mode=ParseMode.HTML)
    await safe(q.answer, "Готово")


async def cmd_trigger(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message; user = update.effective_user
    if msg is None or user is None or msg.chat.type == "private":
        return
    my_rank = await resolve_rank(ctx.bot, msg.chat.id, user.id)
    if my_rank < RANK_SENIOR_MOD:
        await _rank_error(update, RANK_SENIOR_MOD, my_rank); return

    parts = (msg.text or "").split()
    if len(parts) < 2:
        await safe(
            msg.reply_text,
            "<b>Управление триггерами</b>\n"
            "<code>/trigger add слово [warn|mute|kick|ban]</code>\n"
            "<code>/trigger del слово</code>\n"
            "<code>/trigger list</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    sub = parts[1].lower()

    if sub == "add" and len(parts) >= 3:
        word = parts[2].lower()
        action = parts[3].lower() if len(parts) > 3 else "warn"
        if action not in ("warn", "mute", "kick", "ban"):
            await safe(msg.reply_text, "Действия: warn / mute / kick / ban"); return
        await add_trigger(msg.chat.id, word, action)
        await safe(msg.reply_text,
                   f"✅ Триггер <code>{esc(word)}</code> → <b>{action}</b>",
                   parse_mode=ParseMode.HTML)

    elif sub == "del" and len(parts) >= 3:
        await del_trigger(msg.chat.id, parts[2].lower())
        await safe(msg.reply_text, "🗑 Триггер удалён.")

    elif sub == "list":
        rows = await list_triggers(msg.chat.id)
        if not rows:
            await safe(msg.reply_text, "Триггеров нет."); return
        txt = "\n".join(f"• <code>{esc(r['word'])}</code> → {r['action']}" for r in rows)
        await safe(msg.reply_text, f"<b>Триггеры:</b>\n{txt}", parse_mode=ParseMode.HTML)
    else:
        await safe(msg.reply_text, "Некорректно. См. /help")


async def cmd_create_circle(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message; user = update.effective_user
    if msg is None or user is None or msg.chat.type == "private":
        return
    my_rank = await resolve_rank(ctx.bot, msg.chat.id, user.id)
    if my_rank < RANK_JUNIOR_MOD:
        await _rank_error(update, RANK_JUNIOR_MOD, my_rank); return

    parts = (msg.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await safe(msg.reply_text,
                   "Использование: <code>/create_circle Название</code>",
                   parse_mode=ParseMode.HTML)
        return

    name = parts[1].strip()[:60]
    thread_id = msg.message_thread_id or 0

    existing = await get_circle_by_thread(msg.chat.id, thread_id)
    if existing:
        await safe(msg.reply_text,
                   f"В этой теме уже есть кружок: <b>{esc(existing['name'])}</b>",
                   parse_mode=ParseMode.HTML)
        return

    cid = await create_circle(msg.chat.id, name, user.id, thread_id)
    await add_circle_member(cid, user.id)
    await safe(
        msg.reply_text,
        f"✅ Кружок <b>{esc(name)}</b> создан.\n"
        f"Вступить: <code>/join {esc(name)}</code>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_join(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message; user = update.effective_user
    if msg is None or user is None or msg.chat.type == "private":
        return
    parts = (msg.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await safe(msg.reply_text, "Использование: <code>/join Название</code>",
                   parse_mode=ParseMode.HTML); return
    c = await get_circle_by_name(msg.chat.id, parts[1].strip())
    if not c:
        await safe(msg.reply_text, "Кружок не найден."); return
    await add_circle_member(c["id"], user.id)
    await safe(msg.reply_text,
               f"✅ Ты в кружке <b>{esc(c['name'])}</b>.",
               parse_mode=ParseMode.HTML)


async def cmd_leave(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message; user = update.effective_user
    if msg is None or user is None or msg.chat.type == "private":
        return
    parts = (msg.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await safe(msg.reply_text, "Использование: <code>/leave Название</code>",
                   parse_mode=ParseMode.HTML); return
    c = await get_circle_by_name(msg.chat.id, parts[1].strip())
    if not c:
        await safe(msg.reply_text, "Кружок не найден."); return
    await remove_circle_member(c["id"], user.id)
    await safe(msg.reply_text, f"🚪 Ты вышел из <b>{esc(c['name'])}</b>.",
               parse_mode=ParseMode.HTML)


async def cmd_circle_info(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if msg is None or msg.chat.type == "private":
        return
    parts = (msg.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await safe(msg.reply_text, "Использование: <code>/circle_info Название</code>",
                   parse_mode=ParseMode.HTML); return
    c = await get_circle_by_name(msg.chat.id, parts[1].strip())
    if not c:
        await safe(msg.reply_text, "Кружок не найден."); return
    members = await circle_members(c["id"])
    lines = [f"👥 <b>{esc(c['name'])}</b> — {len(members)} участн."]
    for row in members[:50]:
        uid = row["user_id"]
        try:
            m = await ctx.bot.get_chat_member(msg.chat.id, uid)
            lines.append(f"• {m.user.mention_html()}")
        except Exception:
            lines.append(f"• <code>{uid}</code>")
    await safe(msg.reply_text, "\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_delete_circle(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message; user = update.effective_user
    if msg is None or user is None or msg.chat.type == "private":
        return
    my_rank = await resolve_rank(ctx.bot, msg.chat.id, user.id)
    if my_rank < RANK_JUNIOR_MOD:
        await _rank_error(update, RANK_JUNIOR_MOD, my_rank); return
    parts = (msg.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await safe(msg.reply_text, "Использование: <code>/delete_circle Название</code>",
                   parse_mode=ParseMode.HTML); return
    c = await get_circle_by_name(msg.chat.id, parts[1].strip())
    if not c:
        await safe(msg.reply_text, "Кружок не найден."); return
    if c["owner_id"] != user.id and my_rank < RANK_SENIOR_ADMIN:
        await safe(msg.reply_text, "Только владелец кружка или старший админ."); return
    await delete_circle(c["id"])
    await safe(msg.reply_text, f"🗑 Кружок <b>{esc(c['name'])}</b> удалён.",
               parse_mode=ParseMode.HTML)


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
    try:
        await msg.delete()
    except Exception:
        pass
    note_kwargs = {"message_thread_id": thread_id} if thread_id else {}
    note = await safe(
        ctx.bot.send_message, msg.chat.id,
        f"🧹 Удалено: {deleted}",
        **note_kwargs,
    )
    if note is not None:
        await asyncio.sleep(5)
        await safe(ctx.bot.delete_message, msg.chat.id, note.message_id)


async def cmd_links(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message; user = update.effective_user
    if msg is None or user is None or msg.chat.type == "private":
        return
    my_rank = await resolve_rank(ctx.bot, msg.chat.id, user.id)
    if my_rank < RANK_SENIOR_MOD:
        await _rank_error(update, RANK_SENIOR_MOD, my_rank); return

    parts = (msg.text or "").split()
    arg = parts[1].lower() if len(parts) > 1 else ""
    if arg not in ("on", "off"):
        await safe(msg.reply_text,
                   "Использование: <code>/links on</code> или <code>/links off</code>",
                   parse_mode=ParseMode.HTML); return

    thread_id = msg.message_thread_id or 0
    allowed = (arg == "on")
    await set_topic_links(msg.chat.id, thread_id, allowed)
    await safe(msg.reply_text,
               f"🔗 Ссылки в этой теме: <b>{'разрешены' if allowed else 'запрещены'}</b>",
               parse_mode=ParseMode.HTML)


async def cmd_report(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message; user = update.effective_user
    if msg is None or user is None or msg.chat.type == "private":
        return
    if msg.reply_to_message is None or msg.reply_to_message.from_user is None:
        await safe(msg.reply_text,
                   "Ответь на сообщение нарушителя и напиши <code>/report причина</code>",
                   parse_mode=ParseMode.HTML); return

    target = msg.reply_to_message.from_user
    if target.id == user.id or target.is_bot:
        await safe(msg.reply_text, "Нельзя репортить себя или бота."); return

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
        await safe(msg.reply_text, "⚠️ Не смог отправить, попробуй позже."); return
    await safe(msg.reply_text, "✅ Жалоба отправлена модераторам.",
               disable_notification=True)


async def _build_profile(ctx, chat_id: int, target) -> str:
    row = await get_user(target.id, chat_id)
    warns = row["warns"] if row else 0
    rank_val = await resolve_rank(ctx.bot, chat_id, target.id)
    circles = await circles_of_user(chat_id, target.id)
    circle_names = ", ".join(c["name"] for c in circles) if circles else "—"
    lines = [
        "👤 <b>Профиль</b>",
        f"Имя: {target.mention_html()}",
        f"ID: <code>{target.id}</code>",
        f"Ранг: <b>{RANK_NAMES.get(rank_val, '—')}</b>",
        f"⚠ Предупреждений: <b>{warns}/3</b>",
        f"👥 Кружки: {circle_names}",
    ]
    if row and row["mute_until"]:
        lines.append(f"🔇 Мут до: <code>{esc(row['mute_until'])}</code>")
    return "\n".join(lines)


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
    await safe(msg.reply_text,
               await _build_profile(ctx, msg.chat.id, target),
               parse_mode=ParseMode.HTML)


async def cmd_me(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message; user = update.effective_user
    if msg is None or user is None:
        return
    await safe(msg.reply_text,
               await _build_profile(ctx, msg.chat.id, user),
               parse_mode=ParseMode.HTML)


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
        await safe(
            msg.reply_text,
            f"<b>Настройки чата</b>\n"
            f"Антиспам: <b>{'вкл' if s['antispam'] else 'выкл'}</b>\n"
            f"Антирейд: <b>{'вкл' if s['antiraid'] else 'выкл'}</b>\n"
            f"Лимит флуда: <b>{s['flood_limit']}</b> сообщений за <b>{s['flood_window']}</b> сек\n"
            f"Мут за флуд: <b>{s['mute_minutes']}</b> мин\n\n"
            f"<code>/settings antispam on|off</code>\n"
            f"<code>/settings antiraid on|off</code>\n"
            f"<code>/settings flood &lt;N&gt; &lt;сек&gt;</code>\n"
            f"<code>/settings mute &lt;мин&gt;</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    key = parts[1].lower()
    if key in ("antispam", "antiraid") and len(parts) > 2:
        val = 1 if parts[2].lower() in ("on", "1", "вкл") else 0
        await set_setting(msg.chat.id, key, val)
        await safe(msg.reply_text, f"✅ {key} → {'on' if val else 'off'}")
    elif key == "flood" and len(parts) > 3:
        try:
            n, w = int(parts[2]), int(parts[3])
        except ValueError:
            await safe(msg.reply_text, "Числа нужны."); return
        await set_setting(msg.chat.id, "flood_limit", n)
        await set_setting(msg.chat.id, "flood_window", w)
        await safe(msg.reply_text, f"✅ flood → {n}/{w}с")
    elif key == "mute" and len(parts) > 2:
        try:
            m = int(parts[2])
        except ValueError:
            await safe(msg.reply_text, "Число нужно."); return
        await set_setting(msg.chat.id, "mute_minutes", m)
        await safe(msg.reply_text, f"✅ mute → {m} мин")
    else:
        await safe(msg.reply_text, "См. /settings")


async def _post_init(app: Application):
    try:
        await app.bot.set_my_commands([
            BotCommand("help", "Список команд"),
            BotCommand("mod", "Меню модерации (реплай)"),
            BotCommand("report", "Жалоба (реплай)"),
            BotCommand("me", "Мой профиль"),
            BotCommand("profile", "Профиль участника"),
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

    app.add_handler(MessageHandler(G, pre_remember), group=-2)

    app.add_handler(MessageHandler(G & ~filters.COMMAND, antispam_mw), group=-1)
    app.add_handler(MessageHandler(G & (filters.TEXT | filters.CAPTION), link_guard_mw), group=-1)
    app.add_handler(MessageHandler(G & (filters.TEXT | filters.CAPTION), trigger_mw), group=-1)

    app.add_handler(ChatMemberHandler(antiraid_track, chat_member_types=ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(ChatMemberHandler(on_bot_added, chat_member_types=ChatMemberHandler.CHAT_MEMBER))

    app.add_handler(CallbackQueryHandler(cb_mod, pattern=r"^mod:"))

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("ranks", cmd_ranks))
    app.add_handler(CommandHandler("setrank", cmd_setrank, filters=G))

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
