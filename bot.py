import subprocess
import sys


def _ensure(pkg_spec: str, import_name: str) -> None:
    try:
        __import__(import_name)
    except ImportError:
        subprocess.check_call([sys.executable, "-m", "pip", "install", pkg_spec])


_ensure("python-telegram-bot>=22,<24", "telegram")
_ensure("python-dotenv", "dotenv")


import asyncio
import contextvars
import html
import logging
import os
import re
import secrets
import sqlite3
import string
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
ADMIN_THREAD_ID  = _req_int("ADMIN_THREAD_ID", 0) or None
REPORT_CHAT_ID   = _req_int("REPORT_CHAT_ID")
REPORT_THREAD_ID = _req_int("REPORT_THREAD_ID", 0) or None
DB_PATH          = os.getenv("DB_PATH", "bot.db")

DEFAULT_EPHEMERAL_DELAY = 60.0
MIN_TG_MUTE_SEC = 60

CONNECT_TIMEOUT   = 30.0
READ_TIMEOUT      = 30.0
WRITE_TIMEOUT     = 30.0
POOL_TIMEOUT      = 30.0
START_RETRY_DELAY = 10.0

TG_SEND_SEMAPHORE = 20
MAX_PENDING_DELETES = 200
RANK_CACHE_TTL = 60


from telegram import (
    BotCommand,
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import NetworkError, TimedOut, RetryAfter
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

log.info("Config loaded | cwd=%s | admin=%s | admin_thread=%s | report_chat=%s | report_thread=%s",
         Path.cwd(), ADMIN_CHAT_ID, ADMIN_THREAD_ID, REPORT_CHAT_ID, REPORT_THREAD_ID)


_acted = contextvars.ContextVar("worm_acted", default=False)


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

RANK_TAGS = {
    RANK_OWNER:        "Создатель",
    RANK_SENIOR_ADMIN: "Ст. Админ",
    RANK_JUNIOR_ADMIN: "Мл. Админ",
    RANK_SENIOR_MOD:   "Ст. Модератор",
    RANK_JUNIOR_MOD:   "Мл. Модератор",
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


def _gen_key() -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(12))


def _log_activation_key(chat_id: int, chat_title: str, key: str):
    bar = "=" * 60
    log.warning(bar)
    log.warning("  KEY ACTIVATION")
    log.warning("  Chat: %s (%s)", chat_title, chat_id)
    log.warning("  Key:  %s", key)
    log.warning("  Usage in chat: /activate %s", key)
    log.warning(bar)
    try:
        print(bar, flush=True)
        print(f"  KEY ACTIVATION", flush=True)
        print(f"  Chat: {chat_title} ({chat_id})", flush=True)
        print(f"  Key:  {key}", flush=True)
        print(f"  Usage in chat: /activate {key}", flush=True)
        print(bar, flush=True)
    except Exception:
        pass


_pending_tasks: set = set()
_pending_unmutes: dict = {}

_db_write_lock: Optional[asyncio.Lock] = None
_tg_sem: Optional[asyncio.Semaphore] = None
_pending_deletes_count = 0

_rank_cache: dict = {}


def _schedule(coro):
    try:
        task = asyncio.create_task(coro)
        _pending_tasks.add(task)
        task.add_done_callback(_pending_tasks.discard)
    except Exception as e:
        log.debug("_schedule fail: %s", e)


def _cancel_pending_unmute(chat_id: int, user_id: int):
    key = (chat_id, user_id)
    task = _pending_unmutes.pop(key, None)
    if task and not task.done():
        task.cancel()


def _schedule_unmute(bot, chat_id: int, user_id: int, secs: int):
    key = (chat_id, user_id)
    _cancel_pending_unmute(chat_id, user_id)
    task = asyncio.create_task(_unmute_later(bot, chat_id, user_id, secs))
    _pending_unmutes[key] = task
    _pending_tasks.add(task)
    task.add_done_callback(_pending_tasks.discard)

    def _cleanup(t, k=key):
        if _pending_unmutes.get(k) is t:
            _pending_unmutes.pop(k, None)

    task.add_done_callback(_cleanup)


def _cache_rank(chat_id: int, user_id: int, rank: int):
    _rank_cache[(chat_id, user_id)] = (rank, time.monotonic())


def _drop_rank_cache(chat_id: int, user_id: int):
    _rank_cache.pop((chat_id, user_id), None)


def _drop_rank_cache_chat(chat_id: int):
    for k in list(_rank_cache.keys()):
        if k[0] == chat_id:
            _rank_cache.pop(k, None)


async def _send_with_sem(coro_fn, *args, **kwargs):
    global _tg_sem
    if _tg_sem is None:
        _tg_sem = asyncio.Semaphore(TG_SEND_SEMAPHORE)
    async with _tg_sem:
        return await coro_fn(*args, **kwargs)


async def safe(coro_fn, *args, retries: int = 3, **kwargs):
    for attempt in range(1, retries + 1):
        try:
            return await _send_with_sem(coro_fn, *args, **kwargs)
        except RetryAfter as e:
            wait = float(getattr(e, "retry_after", 3))
            if attempt == retries:
                log.debug("safe(): RetryAfter финально — %s", e)
                return None
            await asyncio.sleep(min(wait + 0.5, 30))
        except (TimedOut, NetworkError) as e:
            if attempt == retries:
                log.debug("safe(): окончательно упало — %s", e)
                return None
            await asyncio.sleep(1.5 * attempt)
        except Exception as e:
            msg = str(e).lower()
            if ("not found" in msg
                    or "message to delete" in msg
                    or "message to be replied" in msg
                    or "message can't be deleted" in msg
                    or "chat not found" in msg
                    or "too many requests" in msg):
                log.debug("safe(): %s", e)
            else:
                log.debug("safe(): non-network error — %s", e)
            return None


async def _del_later(bot, chat_id: int, message_id: int, delay: float):
    global _pending_deletes_count
    try:
        await asyncio.sleep(delay)
        ok = await safe(bot.delete_message, chat_id, message_id)
        if ok is not None:
            await forget_messages(chat_id, [message_id])
    finally:
        _pending_deletes_count = max(0, _pending_deletes_count - 1)


async def _delayed(bot, chat_id: int, message_id: int):
    global _pending_deletes_count
    try:
        s = await get_settings(chat_id)
        delay = int(_s(s, "ephemeral_delay", int(DEFAULT_EPHEMERAL_DELAY)))
    except Exception:
        delay = int(DEFAULT_EPHEMERAL_DELAY)
    if delay <= 0:
        return
    if _pending_deletes_count >= MAX_PENDING_DELETES:
        return
    _pending_deletes_count += 1
    _schedule(_del_later(bot, chat_id, message_id, float(delay)))


async def _remember_sent(sent, fallback_thread: int = 0):
    try:
        tid = getattr(sent, "message_thread_id", None) or fallback_thread or 0
        await remember_message(sent.chat.id, sent.message_id, tid, None, 1)
    except Exception as e:
        log.debug("_remember_sent fail: %s", e)


async def eph(msg, text: str, delay: Optional[float] = None, bot=None, **kwargs):
    _acted.set(True)
    sent = await safe(msg.reply_text, text, **kwargs)
    if sent is None:
        return sent
    await _remember_sent(sent, msg.message_thread_id or 0)
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
    if ADMIN_THREAD_ID:
        kw["message_thread_id"] = ADMIN_THREAD_ID
    sent = await safe(bot.send_message, ADMIN_CHAT_ID, text, **kw)
    if sent is None and ADMIN_THREAD_ID:
        kw.pop("message_thread_id", None)
        sent = await safe(bot.send_message, ADMIN_CHAT_ID, text, **kw)
    if sent is None:
        log.debug("send_admin: не удалось доставить лог в %s", ADMIN_CHAT_ID)
    return sent


async def send_report(bot, text: str, **kwargs):
    kw = dict(kwargs)
    if REPORT_THREAD_ID:
        kw["message_thread_id"] = REPORT_THREAD_ID
    sent = await safe(bot.send_message, REPORT_CHAT_ID, text, **kw)
    if sent is None and REPORT_THREAD_ID:
        kw.pop("message_thread_id", None)
        sent = await safe(bot.send_message, REPORT_CHAT_ID, text, **kw)
    if sent is None:
        log.debug("send_report: не удалось доставить репорт в %s", REPORT_CHAT_ID)
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


def _parse_cmd_time(tok: str) -> Optional[int]:
    if not tok:
        return None
    t = tok.strip().lower()
    if t in ("0", "вечно", "навсегда", "forever", "inf"):
        return 0
    secs = parse_duration(t)
    if secs > 0:
        return secs
    return None


LINK_RE = re.compile(r"(https?://|t\.me/|telegram\.me/|@[A-Za-z0-9_]{5,})", re.IGNORECASE)


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA busy_timeout=5000;

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
    is_bot      INTEGER NOT NULL DEFAULT 0,
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

CREATE TABLE IF NOT EXISTS activations (
    chat_id       INTEGER PRIMARY KEY,
    activated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS pending_activations (
    chat_id     INTEGER PRIMARY KEY,
    key         TEXT NOT NULL,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS bot_meta (
    key    TEXT PRIMARY KEY,
    value  TEXT
);

CREATE TABLE IF NOT EXISTS temp_bans (
    chat_id   INTEGER NOT NULL,
    user_id   INTEGER NOT NULL,
    until_ts  INTEGER NOT NULL,
    PRIMARY KEY (chat_id, user_id)
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
    ("default_warn_sec",  "INTEGER NOT NULL DEFAULT 0"),
    ("default_ban_sec",   "INTEGER NOT NULL DEFAULT 0"),
    ("trig_warn_sec",     "INTEGER NOT NULL DEFAULT 0"),
    ("trig_ban_sec",      "INTEGER NOT NULL DEFAULT 0"),
    ("link_action",       "TEXT NOT NULL DEFAULT 'none'"),
]


def _sync_init_db() -> None:
    with sqlite3.connect(DB_PATH, timeout=15.0) as conn:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(SCHEMA)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(settings)")}
        for col, ddl in SETTINGS_MIGRATIONS:
            if col not in cols:
                conn.execute(f"ALTER TABLE settings ADD COLUMN {col} {ddl}")

        msg_cols = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
        if "is_bot" not in msg_cols:
            conn.execute("ALTER TABLE messages ADD COLUMN is_bot INTEGER NOT NULL DEFAULT 0")

        user_cols = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
        if "warn_expires_at" not in user_cols:
            conn.execute("ALTER TABLE users ADD COLUMN warn_expires_at INTEGER")

        mig = conn.execute(
            "SELECT value FROM bot_meta WHERE key='activation_migrated'"
        ).fetchone()
        if mig is None:
            conn.execute(
                "INSERT OR IGNORE INTO activations (chat_id) "
                "SELECT DISTINCT chat_id FROM users"
            )
            conn.execute(
                "INSERT OR IGNORE INTO activations (chat_id) "
                "SELECT chat_id FROM settings"
            )
            conn.execute(
                "INSERT OR REPLACE INTO bot_meta (key, value) "
                "VALUES ('activation_migrated', '1')"
            )
            log.info("Активации: миграция выполнена (старые чаты помечены активными).")

        conn.commit()


def _sync_exec(query: str, params: tuple = (), fetch: Optional[str] = None,
               _retries: int = 4):
    last_err = None
    for attempt in range(_retries):
        try:
            with sqlite3.connect(DB_PATH, timeout=10.0) as conn:
                conn.execute("PRAGMA busy_timeout=5000")
                conn.row_factory = sqlite3.Row
                cur = conn.execute(query, params)
                if fetch == "one":
                    return cur.fetchone()
                if fetch == "all":
                    return cur.fetchall()
                conn.commit()
                return cur.lastrowid
        except sqlite3.OperationalError as e:
            last_err = e
            if "locked" in str(e).lower() and attempt < _retries - 1:
                time.sleep(0.15 * (attempt + 1))
                continue
            raise
    if last_err:
        raise last_err


async def db_exec(query: str, params: tuple = (), fetch: Optional[str] = None):
    global _db_write_lock
    if _db_write_lock is None:
        _db_write_lock = asyncio.Lock()
    if fetch is None:
        async with _db_write_lock:
            return await asyncio.to_thread(_sync_exec, query, params, fetch)
    return await asyncio.to_thread(_sync_exec, query, params, fetch)


async def is_chat_activated(chat_id: int) -> bool:
    row = await db_exec(
        "SELECT chat_id FROM activations WHERE chat_id=?",
        (chat_id,), fetch="one",
    )
    return row is not None


async def activate_chat(chat_id: int):
    await db_exec(
        "INSERT OR IGNORE INTO activations (chat_id) VALUES (?)",
        (chat_id,),
    )


async def get_pending_key(chat_id: int):
    return await db_exec(
        "SELECT key FROM pending_activations WHERE chat_id=?",
        (chat_id,), fetch="one",
    )


async def set_pending_key(chat_id: int, key: str):
    await db_exec(
        "INSERT OR REPLACE INTO pending_activations (chat_id, key) VALUES (?, ?)",
        (chat_id, key),
    )


async def clear_pending_key(chat_id: int):
    await db_exec(
        "DELETE FROM pending_activations WHERE chat_id=?",
        (chat_id,),
    )


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
    _drop_rank_cache(chat_id, user_id)


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


async def add_warn_with_ttl(user_id: int, chat_id: int, ttl_sec: int) -> int:
    row = await get_user(user_id, chat_id)
    prev_warns = int(row["warns"] or 0) if row else 0
    try:
        prev_exp = row["warn_expires_at"] if row else None
    except (IndexError, KeyError):
        prev_exp = None

    total = await add_warn(user_id, chat_id)
    now = int(time.time())

    if not ttl_sec or ttl_sec <= 0:
        new_exp = None
    elif prev_warns == 0:
        new_exp = now + int(ttl_sec)
    elif prev_exp is None:
        new_exp = None
    else:
        new_exp = max(int(prev_exp), now + int(ttl_sec))

    await db_exec(
        "UPDATE users SET warn_expires_at=? WHERE user_id=? AND chat_id=?",
        (new_exp, user_id, chat_id),
    )
    return total


async def reset_warns(user_id: int, chat_id: int):
    await db_exec(
        "UPDATE users SET warns=0, warn_expires_at=NULL WHERE user_id=? AND chat_id=?",
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


async def get_circle_by_name(chat_id: int, thread_id: int, name: str):
    return await db_exec(
        "SELECT * FROM circles WHERE chat_id=? AND thread_id=? AND lower(name)=lower(?)",
        (chat_id, thread_id, name), fetch="one",
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


async def remember_message(chat_id: int, message_id: int, thread_id: int,
                           user_id: Optional[int], is_bot: int = 0):
    await db_exec(
        "INSERT OR REPLACE INTO messages "
        "(chat_id, message_id, thread_id, user_id, is_bot) "
        "VALUES (?, ?, ?, ?, ?)",
        (chat_id, message_id, thread_id, user_id, int(bool(is_bot))),
    )


async def last_messages(chat_id: int, thread_id: int, limit: int,
                        include_bots: bool = True):
    if include_bots:
        query = (
            "SELECT message_id FROM messages "
            "WHERE chat_id=? AND thread_id=? "
            "AND (user_id IS NOT NULL OR is_bot = 1) "
            "ORDER BY message_id DESC LIMIT ?"
        )
    else:
        query = (
            "SELECT message_id FROM messages "
            "WHERE chat_id=? AND thread_id=? AND user_id IS NOT NULL "
            "ORDER BY message_id DESC LIMIT ?"
        )
    return await db_exec(query, (chat_id, thread_id, limit), fetch="all") or []


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
    key = (chat_id, user_id)
    hit = _rank_cache.get(key)
    if hit is not None:
        rank, ts = hit
        if time.monotonic() - ts < RANK_CACHE_TTL:
            return rank

    db_rank = await get_rank(user_id, chat_id)
    if db_rank >= RANK_OWNER:
        _cache_rank(chat_id, user_id, db_rank)
        return db_rank

    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except Exception:
        _cache_rank(chat_id, user_id, db_rank)
        return db_rank

    if member.status == "creator":
        rank = max(db_rank, RANK_OWNER)
    elif member.status == "administrator":
        rank = max(db_rank, RANK_SENIOR_ADMIN)
    else:
        rank = db_rank

    _cache_rank(chat_id, user_id, rank)
    return rank


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


async def _rank_denied(update: Update, min_rank: int, my_rank: int) -> bool:
    if my_rank >= min_rank:
        return False
    if my_rank >= RANK_JUNIOR_MOD:
        await _rank_error(update, min_rank, my_rank)
    return True


async def _deny_higher(msg, target_mention: str, target_rank: int = -1):
    return


async def _no_target(msg):
    return


async def _user_in_chat(bot, chat_id: int, user_id: int) -> bool:
    try:
        m = await bot.get_chat_member(chat_id, user_id)
    except Exception:
        return False
    return m.status in ("member", "administrator", "creator", "restricted")


async def _is_banned(bot, chat_id: int, user_id: int) -> bool:
    try:
        m = await bot.get_chat_member(chat_id, user_id)
    except Exception:
        return False
    return m.status == "kicked"


async def _is_muted(bot, chat_id: int, user_id: int) -> bool:
    try:
        m = await bot.get_chat_member(chat_id, user_id)
    except Exception:
        return False
    if m.status != "restricted":
        return False
    return not bool(getattr(m, "can_send_messages", True))


async def _has_warns(chat_id: int, user_id: int) -> bool:
    row = await get_user(user_id, chat_id)
    return bool(row and row["warns"] and row["warns"] > 0)


async def apply_chat_tag(bot, chat_id: int, user_id: int, rank: int) -> bool:
    tag = RANK_TAGS.get(rank, "")
    try:
        setter = getattr(bot, "set_chat_member_tag", None)
        if setter is None:
            log.warning("apply_chat_tag: set_chat_member_tag недоступен в этой версии PTB")
            return False
        if len(tag) > 16:
            tag = tag[:16]
        await setter(chat_id=chat_id, user_id=user_id, tag=tag)
        return True
    except Exception as e:
        log.debug("apply_chat_tag fail: %s", e)
        return False


async def _mute_user(bot, chat_id: int, user_id: int, secs: int) -> bool:
    secs = int(secs)
    try:
        if secs <= 0:
            _cancel_pending_unmute(chat_id, user_id)
            await bot.restrict_chat_member(chat_id, user_id, MUTE_PERMS)
        elif secs >= MIN_TG_MUTE_SEC:
            await bot.restrict_chat_member(
                chat_id, user_id, MUTE_PERMS,
                until_date=int(time.time()) + secs,
            )
            _schedule_unmute(bot, chat_id, user_id, secs)
        else:
            await bot.restrict_chat_member(chat_id, user_id, MUTE_PERMS)
            _schedule_unmute(bot, chat_id, user_id, secs)
        await set_mute(user_id, chat_id, None)
        return True
    except Exception as e:
        log.debug("_mute_user fail: %s", e)
        return False


async def _unmute_later(bot, chat_id: int, user_id: int, secs: int):
    try:
        await asyncio.sleep(secs)
    except asyncio.CancelledError:
        return
    try:
        await bot.restrict_chat_member(chat_id, user_id, UNMUTE_PERMS)
        await set_mute(user_id, chat_id, None)
        try:
            member = await bot.get_chat_member(chat_id, user_id)
            target = member.user
        except Exception:
            target = user_id
        await log_action(
            bot, None,
            f"🔊 Авто-размут (истёк срок {fmt_seconds(secs)})",
            target, "", chat_id,
        )
    except Exception as e:
        log.debug("auto-unmute fail: %s", e)


async def _ban_user(bot, chat_id: int, user_id: int, secs: int) -> bool:
    try:
        if secs and secs > 0:
            until = int(time.time()) + int(secs)
            await bot.ban_chat_member(chat_id, user_id, until_date=until)
            await db_exec(
                "INSERT OR REPLACE INTO temp_bans (chat_id, user_id, until_ts) "
                "VALUES (?, ?, ?)",
                (chat_id, user_id, until),
            )
        else:
            await bot.ban_chat_member(chat_id, user_id)
            await db_exec(
                "DELETE FROM temp_bans WHERE chat_id=? AND user_id=?",
                (chat_id, user_id),
            )
        return True
    except Exception as e:
        log.debug("_ban_user fail: %s", e)
        return False


async def _apply_warn_limit_action(bot, chat_id: int, user, settings):
    uid = user.id
    warn_limit = int(_s(settings, "warn_limit", 3))
    auto_mute_sec = int(_s(settings, "auto_mute_sec", 3600))
    default_ban = int(_s(settings, "default_ban_sec", 0))
    warn_action = str(_s(settings, "warn_action", "ban")).lower()
    try:
        if warn_action == "ban":
            await _ban_user(bot, chat_id, uid, default_ban)
            await add_blacklist(uid, chat_id, "auto_ban_warns")
            await reset_warns(uid, chat_id)
            tail = f" на {fmt_seconds(default_ban)}" if default_ban > 0 else ""
            await log_action(bot, None,
                             f"🔨 Авто-бан{tail} ({warn_limit}/{warn_limit} варнов)",
                             user, "", chat_id)
            return f"🔨 забанен{tail}"
        elif warn_action == "kick":
            await bot.ban_chat_member(chat_id, uid)
            await bot.unban_chat_member(chat_id, uid)
            await reset_warns(uid, chat_id)
            await log_action(bot, None,
                             f"👢 Авто-кик ({warn_limit}/{warn_limit} варнов)",
                             user, "", chat_id)
            return "👢 кикнут"
        else:
            ok = await _mute_user(bot, chat_id, uid, auto_mute_sec)
            if ok:
                await reset_warns(uid, chat_id)
                tail = f" на {fmt_seconds(auto_mute_sec)}" if auto_mute_sec > 0 else " навсегда"
                await log_action(bot, None,
                                 f"🔇 Авто-мут{tail} ({warn_limit}/{warn_limit} варнов)",
                                 user, "", chat_id)
                return f"🔇 мут{tail}"
    except Exception as e:
        log.debug("apply_warn_limit_action: %s", e)
    return ""


async def _punishment_sweeper(app: Application):
    while True:
        try:
            now = int(time.time())

            warn_rows = await db_exec(
                "SELECT user_id, chat_id, warns FROM users "
                "WHERE warns>0 AND warn_expires_at IS NOT NULL AND warn_expires_at <= ?",
                (now,), fetch="all",
            ) or []
            for r in warn_rows:
                uid = r["user_id"]; cid = r["chat_id"]; cnt = r["warns"]
                try:
                    member = await app.bot.get_chat_member(cid, uid)
                    target = member.user
                except Exception:
                    target = uid
                await log_action(
                    app.bot, None,
                    f"⚠️ Авто-сброс варнов (истёк срок, было {cnt})",
                    target, "", cid,
                )

            await db_exec(
                "UPDATE users SET warns=0, warn_expires_at=NULL "
                "WHERE warns>0 AND warn_expires_at IS NOT NULL AND warn_expires_at <= ?",
                (now,),
            )

            rows = await db_exec(
                "SELECT chat_id, user_id FROM temp_bans WHERE until_ts <= ?",
                (now,), fetch="all",
            ) or []
            for r in rows:
                cid = r["chat_id"]; uid = r["user_id"]
                try:
                    await app.bot.unban_chat_member(cid, uid)
                except Exception:
                    pass
                await remove_blacklist(uid, cid)
                await db_exec(
                    "DELETE FROM temp_bans WHERE chat_id=? AND user_id=?",
                    (cid, uid),
                )
                try:
                    member = await app.bot.get_chat_member(cid, uid)
                    target = member.user
                except Exception:
                    target = uid
                await log_action(
                    app.bot, None,
                    "🔓 Авто-разбан (истёк срок)",
                    target, "", cid,
                )
        except Exception as e:
            log.debug("sweeper: %s", e)
        await asyncio.sleep(60)


async def _housekeeping():
    while True:
        try:
            now = time.monotonic()

            for key in list(_flood_buckets.keys()):
                b = _flood_buckets.get(key)
                if not b:
                    _flood_buckets.pop(key, None)
                    continue
                if now - b[-1] > 300:
                    _flood_buckets.pop(key, None)

            for key in list(_raid_buckets.keys()):
                b = _raid_buckets.get(key)
                if not b:
                    _raid_buckets.pop(key, None)
                    continue
                if now - b[-1] > 3600:
                    _raid_buckets.pop(key, None)

            for key in list(_raid_alerted.keys()):
                if now - _raid_alerted[key] > 7200:
                    _raid_alerted.pop(key, None)

            for key in list(_rank_cache.keys()):
                _, ts = _rank_cache[key]
                if now - ts > RANK_CACHE_TTL * 3:
                    _rank_cache.pop(key, None)

        except Exception as e:
            log.debug("housekeeping: %s", e)
        await asyncio.sleep(300)


def mod_action(fn):
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        msg = update.effective_message
        token = _acted.set(False)
        try:
            await fn(update, ctx)
        finally:
            was_acted = _acted.get()
            _acted.reset(token)
            if was_acted and msg is not None and msg.chat.type != "private":
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


async def activation_mw(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if msg is None or msg.chat.type == "private":
        return

    if msg.chat.id in (ADMIN_CHAT_ID, REPORT_CHAT_ID):
        return

    text = (msg.text or "").strip().lower()
    if text.startswith("/activate") or text.startswith("активация") or text.startswith("activate"):
        return

    if await is_chat_activated(msg.chat.id):
        return

    raise ApplicationHandlerStop


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
    sender = msg.from_user
    if sender:
        await upsert_user(sender.id, msg.chat.id,
                          sender.username, sender.first_name)
    await remember_message(
        msg.chat.id, msg.message_id,
        msg.message_thread_id or 0,
        sender.id if sender else None,
        1 if (sender and sender.is_bot) else 0,
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
        ttl = f" на {fmt_seconds(mute_sec)}" if mute_sec > 0 else " навсегда"
        await eph(
            msg,
            f"🔇 {user.mention_html()} — мут за флуд{ttl}.",
            parse_mode=ParseMode.HTML,
        )
        await log_action(ctx.bot, None, f"🔇 Авто-мут за флуд{ttl}", user, "",
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

    link_action = str(_s(settings, "link_action", "none")).lower()
    warn_limit = int(_s(settings, "warn_limit", 3))
    auto_mute_sec = int(_s(settings, "auto_mute_sec", 3600))
    default_warn_sec = int(_s(settings, "default_warn_sec", 0))
    default_ban_sec = int(_s(settings, "default_ban_sec", 0))

    notice = "🚫 Ссылки в этом чате запрещены."

    if link_action in ("none", "", None):
        await eph(msg, notice, disable_notification=True)
        raise ApplicationHandlerStop

    uid = user.id
    mention = user.mention_html()

    if link_action == "warn":
        total = await add_warn_with_ttl(uid, msg.chat.id, default_warn_sec)
        ttl_txt = f" на {fmt_seconds(default_warn_sec)}" if default_warn_sec > 0 else ""
        notice += f"\n⚠️ {mention} — предупреждение{ttl_txt} ({total}/{warn_limit})."
        await log_action(ctx.bot, None,
                         f"⚠️ Авто-варн за ссылку{ttl_txt} ({total}/{warn_limit})",
                         user, "", msg.chat.id, reply_msg_id=msg.message_id)
        if total >= warn_limit:
            result = await _apply_warn_limit_action(ctx.bot, msg.chat.id, user, settings)
            if result:
                notice += f"\n{result}"

    elif link_action == "mute":
        ok = await _mute_user(ctx.bot, msg.chat.id, uid, auto_mute_sec)
        if ok:
            ttl = f" на {fmt_seconds(auto_mute_sec)}" if auto_mute_sec > 0 else " навсегда"
            notice += f"\n🔇 {mention} — мут{ttl}."
            await log_action(ctx.bot, None, f"🔇 Авто-мут за ссылку{ttl}",
                             user, "", msg.chat.id, reply_msg_id=msg.message_id)

    elif link_action == "kick":
        try:
            await ctx.bot.ban_chat_member(msg.chat.id, uid)
            await ctx.bot.unban_chat_member(msg.chat.id, uid)
            notice += f"\n👢 {mention} — кикнут."
            await log_action(ctx.bot, None, "👢 Авто-кик за ссылку",
                             user, "", msg.chat.id, reply_msg_id=msg.message_id)
        except Exception as e:
            log.debug("link kick fail: %s", e)

    elif link_action == "ban":
        ok = await _ban_user(ctx.bot, msg.chat.id, uid, default_ban_sec)
        if ok:
            await add_blacklist(uid, msg.chat.id, "auto_ban_links")
            ttl = f" на {fmt_seconds(default_ban_sec)}" if default_ban_sec > 0 else " навсегда"
            notice += f"\n🔨 {mention} — забанен{ttl}."
            await log_action(ctx.bot, None, f"🔨 Авто-бан за ссылку{ttl}",
                             user, "", msg.chat.id, reply_msg_id=msg.message_id)

    await eph(msg, notice, disable_notification=True)
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
            thread_id = msg.message_thread_id or 0
            await safe(msg.delete)
            await _apply_trigger(ctx.bot, msg.chat.id, user, action, word, settings, thread_id)
            raise ApplicationHandlerStop


async def _apply_trigger(bot, chat_id: int, user, action: str, word: str, settings,
                         thread_id: int = 0):
    uid = user.id
    mention = user.mention_html()
    trig_warn_limit = int(_s(settings, "trig_warn_limit", 3))
    trig_mute_sec = int(_s(settings, "trig_mute_sec", 3600))
    auto_mute_sec = int(_s(settings, "auto_mute_sec", 3600))
    trig_warn_sec = int(_s(settings, "trig_warn_sec", 0))
    trig_ban_sec = int(_s(settings, "trig_ban_sec", 0))

    async def notify(text: str):
        kw = {"parse_mode": ParseMode.HTML}
        if thread_id:
            kw["message_thread_id"] = thread_id
        note = await safe(bot.send_message, chat_id, text, **kw)
        if note is not None:
            await _remember_sent(note, thread_id)
            await _delayed(bot, chat_id, note.message_id)

    if action == "warn":
        total = await add_warn_with_ttl(uid, chat_id, trig_warn_sec)
        ttl_txt = f" на {fmt_seconds(trig_warn_sec)}" if trig_warn_sec > 0 else ""
        await notify(
            f"⚠️ {mention} — предупреждение{ttl_txt} (триггер: <code>{esc(word)}</code>). "
            f"Всего: {total}/{trig_warn_limit}"
        )
        await log_action(bot, None,
                         f"⚠️ Авто-варн{ttl_txt} (триггер: {word}) {total}/{trig_warn_limit}",
                         user, "", chat_id)
        if total >= trig_warn_limit:
            warn_action = str(_s(settings, "warn_action", "ban")).lower()
            try:
                if warn_action == "ban":
                    await _ban_user(bot, chat_id, uid, trig_ban_sec)
                    await add_blacklist(uid, chat_id, "auto_ban_warns")
                    await reset_warns(uid, chat_id)
                    ban_txt = f" на {fmt_seconds(trig_ban_sec)}" if trig_ban_sec > 0 else ""
                    await notify(
                        f"🔨 {mention} — забанен{ban_txt} "
                        f"({trig_warn_limit}/{trig_warn_limit} варнов)."
                    )
                    await log_action(bot, None,
                                     f"🔨 Авто-бан{ban_txt} ({trig_warn_limit}/{trig_warn_limit} варнов по триггеру)",
                                     user, "", chat_id)
                elif warn_action == "kick":
                    await bot.ban_chat_member(chat_id, uid)
                    await bot.unban_chat_member(chat_id, uid)
                    await reset_warns(uid, chat_id)
                    await notify(
                        f"👢 {mention} — кикнут "
                        f"({trig_warn_limit}/{trig_warn_limit} варнов)."
                    )
                    await log_action(bot, None,
                                     f"👢 Авто-кик ({trig_warn_limit}/{trig_warn_limit} варнов по триггеру)",
                                     user, "", chat_id)
                else:
                    await _mute_user(bot, chat_id, uid, auto_mute_sec)
                    await reset_warns(uid, chat_id)
                    mute_ttl = f" на {fmt_seconds(auto_mute_sec)}" if auto_mute_sec > 0 else " навсегда"
                    await notify(
                        f"🔇 {mention} — авто-мут{mute_ttl} "
                        f"({trig_warn_limit}/{trig_warn_limit})."
                    )
                    await log_action(bot, None,
                                     f"🔇 Авто-мут{mute_ttl} ({trig_warn_limit}/{trig_warn_limit} варна)",
                                     user, "", chat_id)
            except Exception as e:
                log.debug("auto-warn-action fail (trigger): %s", e)

    elif action == "mute":
        await _mute_user(bot, chat_id, uid, trig_mute_sec)
        mute_ttl = f" на {fmt_seconds(trig_mute_sec)}" if trig_mute_sec > 0 else " навсегда"
        await notify(
            f"🔇 {mention} — мут{mute_ttl} "
            f"(триггер: <code>{esc(word)}</code>)."
        )
        await log_action(bot, None,
                         f"🔇 Авто-мут{mute_ttl} (триггер: {word})",
                         user, "", chat_id)

    elif action == "kick":
        try:
            await bot.ban_chat_member(chat_id, uid)
            await bot.unban_chat_member(chat_id, uid)
            await notify(
                f"👢 {mention} — кикнут (триггер: <code>{esc(word)}</code>)."
            )
            await log_action(bot, None, f"👢 Авто-кик (триггер: {word})", user, "", chat_id)
        except Exception as e:
            log.debug("kick fail: %s", e)

    elif action == "ban":
        try:
            await _ban_user(bot, chat_id, uid, trig_ban_sec)
            ban_txt = f" на {fmt_seconds(trig_ban_sec)}" if trig_ban_sec > 0 else ""
            await notify(
                f"🔨 {mention} — забанен{ban_txt} (триггер: <code>{esc(word)}</code>)."
            )
            await log_action(bot, None, f"🔨 Авто-бан{ban_txt} (триггер: {word})",
                             user, "", chat_id)
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


async def on_member_left(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cm = update.chat_member
    if cm is None:
        return

    me = await ctx.bot.get_me()
    u = cm.new_chat_member.user
    if u.id == me.id or u.is_bot:
        return

    old_status = cm.old_chat_member.status
    new_status = cm.new_chat_member.status

    if new_status not in ("kicked", "left"):
        return
    if old_status in ("left", "kicked"):
        return

    chat_id = cm.chat.id
    user_id = u.id

    row = await get_user(user_id, chat_id)
    if not row or not row["rank"]:
        return

    old_rank = row["rank"]

    await set_rank(user_id, chat_id, RANK_USER)
    await apply_chat_tag(ctx.bot, chat_id, user_id, RANK_USER)

    if new_status == "kicked":
        action = "🔨 Авто-снятие ранга (бан)"
    else:
        action = "🚪 Авто-снятие ранга (выход)"

    await log_action(
        ctx.bot, None,
        f"{action}: было {RANK_NAMES.get(old_rank, '—')}",
        u, "", chat_id,
    )


async def on_bot_added(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cm = update.my_chat_member or update.chat_member
    if cm is None:
        return

    me = await ctx.bot.get_me()
    if cm.new_chat_member.user.id != me.id:
        return

    old_status = cm.old_chat_member.status
    new_status = cm.new_chat_member.status

    if not (old_status in ("left", "kicked")
            and new_status in ("member", "administrator", "restricted")):
        return

    if cm.chat.type == "private":
        return

    chat_id = cm.chat.id

    if chat_id in (ADMIN_CHAT_ID, REPORT_CHAT_ID):
        log.info("Бот добавлен в служебный чат %s — активация не требуется", chat_id)
        return

    chat_title = cm.chat.title or cm.chat.full_name or str(chat_id)

    if await is_chat_activated(chat_id):
        log.info("Бот снова добавлен в уже активный чат %s (%s)", chat_id, chat_title)
        return

    existing = await get_pending_key(chat_id)
    if existing:
        log.info("Ключ для чата %s уже сгенерирован, повторную генерацию пропускаю", chat_id)
        return

    adder = cm.from_user
    if adder is None or adder.is_bot:
        log.warning("Не смог определить, кто добавил бота в %s", chat_id)
        return

    current = await get_rank(adder.id, chat_id)
    if current < RANK_OWNER:
        await set_rank(adder.id, chat_id, RANK_OWNER)
        log.info("Bot added to %s by %s (%s) → OWNER",
                 chat_id, adder.id, adder.username or adder.first_name)

    key = _gen_key()
    await set_pending_key(chat_id, key)
    _log_activation_key(chat_id, chat_title, key)


async def cmd_activate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    if msg is None or user is None or msg.chat.type == "private":
        return

    chat_id = msg.chat.id

    if chat_id in (ADMIN_CHAT_ID, REPORT_CHAT_ID):
        await safe(msg.delete)
        return

    if await is_chat_activated(chat_id):
        await safe(msg.delete)
        return

    parts = (msg.text or "").split()
    if len(parts) < 2:
        await safe(msg.delete)
        return

    entered = parts[1].strip().upper()
    row = await get_pending_key(chat_id)
    if not row:
        await safe(msg.delete)
        return

    expected = (row["key"] or "").upper()
    if entered != expected:
        await safe(msg.delete)
        return

    await activate_chat(chat_id)
    await clear_pending_key(chat_id)
    await safe(msg.delete)

    log.info("Чат %s (%s) активирован пользователем %s",
             chat_id, msg.chat.title or "?", user.id)

    await log_action(ctx.bot, None,
                     "🔓 Чат активирован",
                     user, "", chat_id)

    await eph(
        msg,
        "✅ <b>Чат активирован!</b>\n"
        "Теперь бот работает в полную силу.\n"
        f"Список команд: <code>помощь</code>",
        parse_mode=ParseMode.HTML,
    )


HELP_HEADER = "🛠 <b>Модератор-бот</b>\n\n"

HELP_BLOCKS = {
    "ranks_senior_admin": (
        "<b>Управление рангами:</b>\n"
        "• <code>выдатьранг алиас</code> — реплай на сообщение\n"
        "• <code>снятьранг</code> — реплай на сообщение\n"
        "• <code>роли</code> — кто в чате имеет ранг\n"
        "• <code>ранги</code> — список доступных алиасов\n\n",
        RANK_SENIOR_ADMIN,
    ),
    "settings": (
        "<b>Настройки авто-действий:</b>\n"
        "• <code>настройки</code> — текущие значения и справка\n\n",
        RANK_SENIOR_ADMIN,
    ),
    "mod_junior_admin": (
        "<b>Админ-команды (реплай на сообщение):</b>\n"
        "• <code>бан [1д] [причина]</code>\n"
        "• <code>разбан</code>\n\n",
        RANK_JUNIOR_ADMIN,
    ),
    "mod_senior_mod": (
        "<b>Старший модератор:</b>\n"
        "• <code>триггер добавить слово [варн|мут|кик|бан]</code>\n"
        "• <code>триггер удалить слово</code>\n"
        "• <code>триггер список</code>\n"
        "• <code>ссылки вкл</code> / <code>ссылки выкл</code> — для текущей темы\n\n",
        RANK_SENIOR_MOD,
    ),
    "mod_junior_mod": (
        "<b>Модерация (реплай на сообщение):</b>\n"
        "• <code>мод</code> — меню кнопок\n"
        "• <code>кик</code>\n"
        "• <code>мут 10м</code> (без времени — дефолт из настроек)\n"
        "• <code>размут</code>\n"
        "• <code>варн [1ч]</code> (без времени — дефолт из настроек)\n"
        "• <code>анварн</code>\n"
        "• <code>варны</code>\n"
        "• <code>чистка N</code> — удалить N последних сообщений\n"
        "• <code>ранги</code> — список доступных алиасов\n\n"
        "<b>Кружки:</b>\n"
        "• <code>создатькружок имя</code>\n"
        "• <code>удалитькружок имя</code>\n\n",
        RANK_JUNIOR_MOD,
    ),
    "everyone": (
        "<b>Доступно всем:</b>\n"
        "• <code>репорт причина</code> — жалоба (реплай)\n"
        "• <code>профиль</code> — профиль (реплай) или себя\n"
        "• <code>я</code> — свой профиль\n"
        "• <code>вступить имя</code> — войти в кружок\n"
        "• <code>выйти имя</code> — покинуть кружок\n"
        "• <code>инфокружка имя</code> — состав кружка\n"
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
    if await _rank_denied(update, RANK_JUNIOR_MOD, my_rank):
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
    if await _rank_denied(update, RANK_SENIOR_ADMIN, my_rank):
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


async def _target_user(msg):
    if not msg.reply_to_message or not msg.reply_to_message.from_user:
        return None
    thread_id = msg.message_thread_id or 0
    if thread_id and msg.reply_to_message.message_id == thread_id:
        return None
    t = msg.reply_to_message.from_user
    if msg.from_user and t.id == msg.from_user.id:
        return None
    if t.is_bot:
        return None
    return t


def require_reply(fn):
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        msg = update.effective_message
        if msg is None or msg.chat.type == "private":
            return
        if await _target_user(msg) is None:
            return
        return await fn(update, ctx)
    return wrapper


@require_reply
@mod_action
async def cmd_setrank(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    if msg is None or user is None or msg.chat.type == "private":
        return

    my_rank = await resolve_rank(ctx.bot, msg.chat.id, user.id)
    if await _rank_denied(update, RANK_SENIOR_ADMIN, my_rank):
        return

    parts = (msg.text or "").split()
    if len(parts) != 2:
        return

    key = parts[1].lower().strip()
    if key not in RANK_ALIASES:
        return

    rank = RANK_ALIASES[key]
    if rank >= my_rank:
        await eph(
            msg,
            f"⛔ Нельзя выдать ранг <b>{RANK_NAMES.get(rank, '—')}</b> — "
            f"он не ниже твоего (<b>{RANK_NAMES.get(my_rank, '—')}</b>).",
            parse_mode=ParseMode.HTML,
        )
        return

    target = msg.reply_to_message.from_user
    target_rank = await resolve_rank(ctx.bot, msg.chat.id, target.id)
    if target_rank >= my_rank:
        tr = RANK_NAMES.get(target_rank, "—")
        await eph(
            msg,
            f"⛔ Нельзя менять ранг: {target.mention_html()}\n"
            f"его ранг: <b>{tr}</b> — не ниже твоего.",
            parse_mode=ParseMode.HTML,
        )
        return

    await set_rank(target.id, msg.chat.id, rank)
    tag_ok = await apply_chat_tag(ctx.bot, msg.chat.id, target.id, rank)
    await log_action(ctx.bot, user, f"👑 Выдача ранга: {RANK_NAMES[rank]}", target, "",
                     msg.chat.id, reply_msg_id=msg.reply_to_message.message_id)

    tail = "" if tag_ok else "\n<i>(тег не удалось поставить — проверь, что бот админ и у него есть право «Управление тегами»)</i>"
    await eph(
        msg,
        f"✅ Выдано: {target.mention_html()} → <b>{RANK_NAMES[rank]}</b>{tail}",
        parse_mode=ParseMode.HTML,
    )


@require_reply
@mod_action
async def cmd_unrank(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    if msg is None or user is None or msg.chat.type == "private":
        return

    my_rank = await resolve_rank(ctx.bot, msg.chat.id, user.id)
    if await _rank_denied(update, RANK_SENIOR_ADMIN, my_rank):
        return

    parts = (msg.text or "").split()
    if len(parts) != 1:
        return

    target = msg.reply_to_message.from_user
    target_rank = await resolve_rank(ctx.bot, msg.chat.id, target.id)
    if target_rank >= my_rank:
        tr = RANK_NAMES.get(target_rank, "—")
        await eph(
            msg,
            f"⛔ Нельзя снять ранг: {target.mention_html()}\n"
            f"его ранг: <b>{tr}</b> — не ниже твоего.",
            parse_mode=ParseMode.HTML,
        )
        return

    await set_rank(target.id, msg.chat.id, RANK_USER)
    await apply_chat_tag(ctx.bot, msg.chat.id, target.id, RANK_USER)
    await log_action(ctx.bot, user, "👑 Снятие ранга", target, "", msg.chat.id,
                     reply_msg_id=msg.reply_to_message.message_id)
    await eph(
        msg,
        f"✅ Снято: {target.mention_html()} → <b>{RANK_NAMES[RANK_USER]}</b>",
        parse_mode=ParseMode.HTML,
    )


@require_reply
@mod_action
async def cmd_ban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message; user = update.effective_user
    if msg is None or user is None or msg.chat.type == "private":
        return
    my_rank = await resolve_rank(ctx.bot, msg.chat.id, user.id)
    if await _rank_denied(update, RANK_JUNIOR_ADMIN, my_rank):
        return

    parts = (msg.text or "").split()
    if len(parts) == 1:
        parsed = None
    elif len(parts) == 2:
        parsed = _parse_cmd_time(parts[1])
        if parsed is None:
            return
    else:
        parsed = _parse_cmd_time(parts[1])
        if parsed is None:
            return

    t = await _target_user(msg)
    if not t:
        return
    if not await _user_in_chat(ctx.bot, msg.chat.id, t.id):
        return
    if not await can_act_on(ctx.bot, msg.chat.id, user.id, t.id):
        return

    settings = await get_settings(msg.chat.id)
    default_ban = int(_s(settings, "default_ban_sec", 0))

    if len(parts) == 1:
        secs = default_ban
        reason = "—"
    else:
        secs = parsed if parsed is not None else 0
        reason = " ".join(parts[2:]).strip() or "—"

    reply_id = msg.reply_to_message.message_id if msg.reply_to_message else None
    ok = await _ban_user(ctx.bot, msg.chat.id, t.id, secs)
    if not ok:
        await eph(msg, "❌ Не удалось забанить."); return

    await add_blacklist(t.id, msg.chat.id, "banned")
    ttl_txt = f" на {fmt_seconds(secs)}" if secs > 0 else " навсегда"
    await log_action(ctx.bot, user, f"🔨 Бан{ttl_txt}", t, reason, msg.chat.id,
                     reply_msg_id=reply_id)
    await eph(
        msg,
        f"🔨 Готово: {t.mention_html()} забанен{ttl_txt}.\nПричина: {esc(reason)}",
        parse_mode=ParseMode.HTML,
    )


@require_reply
@mod_action
async def cmd_unban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message; user = update.effective_user
    if msg is None or user is None or msg.chat.type == "private":
        return
    my_rank = await resolve_rank(ctx.bot, msg.chat.id, user.id)
    if await _rank_denied(update, RANK_JUNIOR_ADMIN, my_rank):
        return

    parts = (msg.text or "").split()
    if len(parts) != 1:
        return

    t = await _target_user(msg)
    if not t:
        return

    if not await _is_banned(ctx.bot, msg.chat.id, t.id):
        await eph(msg, f"ℹ️ {t.mention_html()} не в бане — снимать нечего.",
                  parse_mode=ParseMode.HTML)
        return

    reply_id = msg.reply_to_message.message_id if msg.reply_to_message else None
    try:
        await ctx.bot.unban_chat_member(msg.chat.id, t.id)
        await remove_blacklist(t.id, msg.chat.id)
        await db_exec(
            "DELETE FROM temp_bans WHERE chat_id=? AND user_id=?",
            (msg.chat.id, t.id),
        )
        await log_action(ctx.bot, user, "🔓 Разбан", t, "", msg.chat.id,
                         reply_msg_id=reply_id)
        await eph(msg, f"✅ Готово: {t.mention_html()} разбанен.",
                  parse_mode=ParseMode.HTML)
    except Exception as e:
        await eph(msg, f"❌ Ошибка: {esc(e)}")


@require_reply
@mod_action
async def cmd_kick(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message; user = update.effective_user
    if msg is None or user is None or msg.chat.type == "private":
        return
    my_rank = await resolve_rank(ctx.bot, msg.chat.id, user.id)
    if await _rank_denied(update, RANK_JUNIOR_MOD, my_rank):
        return

    parts = (msg.text or "").split()
    if len(parts) != 1:
        return

    t = await _target_user(msg)
    if not t:
        return
    if not await _user_in_chat(ctx.bot, msg.chat.id, t.id):
        return
    if not await can_act_on(ctx.bot, msg.chat.id, user.id, t.id):
        return

    reply_id = msg.reply_to_message.message_id if msg.reply_to_message else None
    try:
        await ctx.bot.ban_chat_member(msg.chat.id, t.id)
        await ctx.bot.unban_chat_member(msg.chat.id, t.id)
        await log_action(ctx.bot, user, "👢 Кик", t, "", msg.chat.id, reply_msg_id=reply_id)
        await eph(msg, f"👢 Готово: {t.mention_html()} кикнут.",
                  parse_mode=ParseMode.HTML)
    except Exception as e:
        await eph(msg, f"❌ Ошибка: {esc(e)}")


@require_reply
@mod_action
async def cmd_mute(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message; user = update.effective_user
    if msg is None or user is None or msg.chat.type == "private":
        return
    my_rank = await resolve_rank(ctx.bot, msg.chat.id, user.id)
    if await _rank_denied(update, RANK_JUNIOR_MOD, my_rank):
        return

    parts = (msg.text or "").split()
    if len(parts) == 1:
        parsed = None
    elif len(parts) == 2:
        parsed = _parse_cmd_time(parts[1])
        if parsed is None:
            return
    else:
        return

    t = await _target_user(msg)
    if not t:
        return
    if not await _user_in_chat(ctx.bot, msg.chat.id, t.id):
        return
    if not await can_act_on(ctx.bot, msg.chat.id, user.id, t.id):
        return

    settings = await get_settings(msg.chat.id)
    if len(parts) == 1:
        secs = int(_s(settings, "default_mute_sec", 600))
    else:
        secs = parsed if parsed is not None else 0

    reply_id = msg.reply_to_message.message_id if msg.reply_to_message else None
    ok = await _mute_user(ctx.bot, msg.chat.id, t.id, secs)
    if ok:
        ttl = f" на {fmt_seconds(secs)}" if secs > 0 else " навсегда"
        await log_action(ctx.bot, user, f"🔇 Мут{ttl}", t, "", msg.chat.id,
                         reply_msg_id=reply_id)
        await eph(msg, f"🔇 Готово: {t.mention_html()} замучен{ttl}.",
                  parse_mode=ParseMode.HTML)
    else:
        await eph(msg, "❌ Не удалось замутить.")


@require_reply
@mod_action
async def cmd_unmute(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message; user = update.effective_user
    if msg is None or user is None or msg.chat.type == "private":
        return
    my_rank = await resolve_rank(ctx.bot, msg.chat.id, user.id)
    if await _rank_denied(update, RANK_JUNIOR_MOD, my_rank):
        return

    parts = (msg.text or "").split()
    if len(parts) != 1:
        return

    t = await _target_user(msg)
    if not t:
        return
    if not await _user_in_chat(ctx.bot, msg.chat.id, t.id):
        return
    if not await can_act_on(ctx.bot, msg.chat.id, user.id, t.id):
        return

    if not await _is_muted(ctx.bot, msg.chat.id, t.id):
        await eph(msg, f"ℹ️ {t.mention_html()} не в муте — снимать нечего.",
                  parse_mode=ParseMode.HTML)
        return

    reply_id = msg.reply_to_message.message_id if msg.reply_to_message else None
    try:
        _cancel_pending_unmute(msg.chat.id, t.id)
        await ctx.bot.restrict_chat_member(msg.chat.id, t.id, UNMUTE_PERMS)
        await set_mute(t.id, msg.chat.id, None)
        await log_action(ctx.bot, user, "🔊 Размут", t, "", msg.chat.id, reply_msg_id=reply_id)
        await eph(msg, f"🔊 Готово: {t.mention_html()} размучен.",
                  parse_mode=ParseMode.HTML)
    except Exception as e:
        await eph(msg, f"❌ Ошибка: {esc(e)}")


@require_reply
@mod_action
async def cmd_warn(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message; user = update.effective_user
    if msg is None or user is None or msg.chat.type == "private":
        return
    my_rank = await resolve_rank(ctx.bot, msg.chat.id, user.id)
    if await _rank_denied(update, RANK_JUNIOR_MOD, my_rank):
        return

    parts = (msg.text or "").split()
    if len(parts) == 1:
        parsed = None
    elif len(parts) == 2:
        parsed = _parse_cmd_time(parts[1])
        if parsed is None:
            return
    else:
        return

    t = await _target_user(msg)
    if not t:
        return
    if not await _user_in_chat(ctx.bot, msg.chat.id, t.id):
        return
    if not await can_act_on(ctx.bot, msg.chat.id, user.id, t.id):
        return

    settings = await get_settings(msg.chat.id)
    warn_limit = int(_s(settings, "warn_limit", 3))
    default_warn = int(_s(settings, "default_warn_sec", 0))

    if len(parts) == 1:
        secs = default_warn
    else:
        secs = parsed if parsed is not None else 0

    reply_id = msg.reply_to_message.message_id if msg.reply_to_message else None
    total = await add_warn_with_ttl(t.id, msg.chat.id, secs)
    ttl_txt = f" на {fmt_seconds(secs)}" if secs > 0 else ""
    await log_action(ctx.bot, user, f"⚠️ Варн{ttl_txt} ({total}/{warn_limit})",
                     t, "", msg.chat.id, reply_msg_id=reply_id)
    await eph(msg,
              f"⚠️ Готово: {t.mention_html()} — предупреждение{ttl_txt} ({total}/{warn_limit}).",
              parse_mode=ParseMode.HTML)

    if total >= warn_limit:
        result = await _apply_warn_limit_action(ctx.bot, msg.chat.id, t, settings)
        if result:
            await eph(msg, f"{t.mention_html()} — {result}.", parse_mode=ParseMode.HTML)


@require_reply
@mod_action
async def cmd_unwarn(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message; user = update.effective_user
    if msg is None or user is None or msg.chat.type == "private":
        return
    my_rank = await resolve_rank(ctx.bot, msg.chat.id, user.id)
    if await _rank_denied(update, RANK_JUNIOR_MOD, my_rank):
        return

    parts = (msg.text or "").split()
    if len(parts) != 1:
        return

    t = await _target_user(msg)
    if not t:
        return
    if not await _user_in_chat(ctx.bot, msg.chat.id, t.id):
        return
    if not await can_act_on(ctx.bot, msg.chat.id, user.id, t.id):
        return

    if not await _has_warns(msg.chat.id, t.id):
        await eph(msg, f"ℹ️ У {t.mention_html()} нет варнов — сбрасывать нечего.",
                  parse_mode=ParseMode.HTML)
        return

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
    exp = None
    try:
        exp = row["warn_expires_at"] if row else None
    except (IndexError, KeyError):
        exp = None
    tail = ""
    if warns and exp:
        left = int(exp) - int(time.time())
        if left > 0:
            tail = f" (сгорят через {fmt_seconds(left)})"
    await eph(msg, f"⚠️ {t.mention_html()}: {warns}/{warn_limit} предупреждений{tail}.",
              parse_mode=ParseMode.HTML)


@require_reply
@mod_action
async def cmd_mod(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message; user = update.effective_user
    if msg is None or user is None or msg.chat.type == "private":
        return
    my_rank = await resolve_rank(ctx.bot, msg.chat.id, user.id)
    if await _rank_denied(update, RANK_JUNIOR_MOD, my_rank):
        return

    parts = (msg.text or "").split()
    if len(parts) != 1:
        return

    t = await _target_user(msg)
    if not t:
        return
    if not await _user_in_chat(ctx.bot, msg.chat.id, t.id):
        return
    _acted.set(True)
    sent = await safe(
        msg.reply_text,
        f"🛡 <b>Модерация</b>\n"
        f"Цель: {t.mention_html()} (<code>{t.id}</code>)",
        parse_mode=ParseMode.HTML,
        reply_markup=mod_kb(msg.chat.id, t.id),
    )
    if sent is not None:
        await _remember_sent(sent, msg.message_thread_id or 0)
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

    if action in ("ban", "kick", "mute", "warn", "unmute", "unwarn", "unban"):
        try:
            m = await ctx.bot.get_chat_member(chat_id, target_id)
            if m.user and m.user.is_bot:
                await safe(q.answer, "⛔ Ботов нельзя", show_alert=True)
                return
        except Exception:
            pass

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
    default_warn = int(_s(settings, "default_warn_sec", 0))
    default_ban = int(_s(settings, "default_ban_sec", 0))

    try:
        member = await ctx.bot.get_chat_member(chat_id, target_id)
        mention = member.user.mention_html()
        target_user_obj = member.user
    except Exception:
        mention = f"<code>{target_id}</code>"
        target_user_obj = None

    result = ""

    if action == "warn":
        total = await add_warn_with_ttl(target_id, chat_id, default_warn)
        ttl_txt = f" на {fmt_seconds(default_warn)}" if default_warn > 0 else ""
        await log_action(ctx.bot, q.from_user, f"⚠️ Варн{ttl_txt} ({total}/{warn_limit}) [кнопка]",
                         target_user_obj or target_id, "", chat_id)
        result = f"⚠️ {mention} — варн{ttl_txt} ({total}/{warn_limit})"
        if total >= warn_limit and target_user_obj is not None:
            extra = await _apply_warn_limit_action(ctx.bot, chat_id, target_user_obj, settings)
            if extra:
                result += f" → {extra}"

    elif action == "mute":
        ok = await _mute_user(ctx.bot, chat_id, target_id, default_mute_sec)
        if ok:
            await log_action(ctx.bot, q.from_user,
                             f"🔇 Мут {fmt_seconds(default_mute_sec) if default_mute_sec > 0 else 'навсегда'} [кнопка]",
                             target_user_obj or target_id, "", chat_id)
            mute_ttl = f" на {fmt_seconds(default_mute_sec)}" if default_mute_sec > 0 else " навсегда"
            result = f"🔇 {mention} — мут{mute_ttl}"
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
            await _ban_user(ctx.bot, chat_id, target_id, default_ban)
            await add_blacklist(target_id, chat_id, "banned_via_button")
            await log_action(ctx.bot, q.from_user, "🔨 Бан [кнопка]",
                             target_user_obj or target_id, "", chat_id)
            ban_txt = f" на {fmt_seconds(default_ban)}" if default_ban > 0 else " навсегда"
            result = f"🔨 {mention} — забанен{ban_txt}"
        except Exception as e:
            result = f"❌ бан: {esc(e)}"

    elif action == "unban":
        if not await _is_banned(ctx.bot, chat_id, target_id):
            result = f"ℹ️ {mention} — не в бане"
        else:
            try:
                await ctx.bot.unban_chat_member(chat_id, target_id)
                await remove_blacklist(target_id, chat_id)
                await db_exec(
                    "DELETE FROM temp_bans WHERE chat_id=? AND user_id=?",
                    (chat_id, target_id),
                )
                await log_action(ctx.bot, q.from_user, "🔓 Разбан [кнопка]",
                                 target_user_obj or target_id, "", chat_id)
                result = f"🔓 {mention} — разбанен"
            except Exception as e:
                result = f"❌ разбан: {esc(e)}"

    elif action == "unmute":
        if not await _is_muted(ctx.bot, chat_id, target_id):
            result = f"ℹ️ {mention} — не в муте"
        else:
            try:
                _cancel_pending_unmute(chat_id, target_id)
                await ctx.bot.restrict_chat_member(chat_id, target_id, UNMUTE_PERMS)
                await set_mute(target_id, chat_id, None)
                await log_action(ctx.bot, q.from_user, "🔊 Размут [кнопка]",
                                 target_user_obj or target_id, "", chat_id)
                result = f"🔊 {mention} — размучен"
            except Exception as e:
                result = f"❌ размут: {esc(e)}"

    elif action == "unwarn":
        if not await _has_warns(chat_id, target_id):
            result = f"ℹ️ {mention} — нет варнов"
        else:
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
            await _remember_sent(note, target_thread)
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
        await _remember_sent(note, q.message.message_thread_id or 0)
        await _delayed(ctx.bot, note.chat.id, note.message_id)
    await safe(q.answer, "Готово")


@mod_action
async def cmd_trigger(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message; user = update.effective_user
    if msg is None or user is None or msg.chat.type == "private":
        return
    my_rank = await resolve_rank(ctx.bot, msg.chat.id, user.id)
    if await _rank_denied(update, RANK_SENIOR_MOD, my_rank):
        return

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
    if await _rank_denied(update, RANK_JUNIOR_MOD, my_rank):
        return

    parts = (msg.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await eph(msg, "❌ Использование: <code>создатькружок Название</code>",
                  parse_mode=ParseMode.HTML)
        return

    name = parts[1].strip()[:60]
    thread_id = msg.message_thread_id or 0

    existing = await get_circle_by_name(msg.chat.id, thread_id, name)
    if existing:
        where = "в этой теме" if thread_id else "в этом чате"
        await eph(msg, f"❌ {where} уже есть кружок <b>{esc(name)}</b>.",
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
    thread_id = msg.message_thread_id or 0
    c = await get_circle_by_name(msg.chat.id, thread_id, parts[1].strip())
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
    thread_id = msg.message_thread_id or 0
    c = await get_circle_by_name(msg.chat.id, thread_id, parts[1].strip())
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
    thread_id = msg.message_thread_id or 0
    c = await get_circle_by_name(msg.chat.id, thread_id, parts[1].strip())
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
    if await _rank_denied(update, RANK_JUNIOR_MOD, my_rank):
        return
    parts = (msg.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await eph(msg, "❌ Использование: <code>удалитькружок Название</code>",
                  parse_mode=ParseMode.HTML); return
    thread_id = msg.message_thread_id or 0
    c = await get_circle_by_name(msg.chat.id, thread_id, parts[1].strip())
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
    if await _rank_denied(update, RANK_JUNIOR_MOD, my_rank):
        return

    thread_id = msg.message_thread_id or 0
    parts = (msg.text or "").split()
    try:
        n = int(parts[1]) if len(parts) > 1 else 30
    except ValueError:
        n = 30
    n = max(1, min(n, 100))

    rows = await last_messages(msg.chat.id, thread_id, n, include_bots=True)
    ids = [r["message_id"] for r in rows]

    deleted = 0
    failed = 0
    for mid in ids:
        try:
            await ctx.bot.delete_message(msg.chat.id, mid)
            deleted += 1
        except Exception as e:
            failed += 1
            text = str(e).lower()
            if "not found" in text or "message to delete" in text:
                pass
            else:
                log.debug("clean delete fail mid=%s: %s", mid, e)
        await asyncio.sleep(0.04)

    await forget_messages(msg.chat.id, ids)
    await log_action(ctx.bot, user,
                     f"🧹 Чистка: {deleted} сообщ. (пропущено {failed})",
                     "—", "", msg.chat.id)

    note_kwargs = {"message_thread_id": thread_id} if thread_id else {}
    note = await safe(
        ctx.bot.send_message, msg.chat.id,
        f"🧹 Удалено: <b>{deleted}</b>"
        + (f"\n⚠️ Не удалось: <b>{failed}</b>" if failed else ""),
        parse_mode=ParseMode.HTML,
        **note_kwargs,
    )
    if note is not None:
        await _remember_sent(note, thread_id)
        await _delayed(ctx.bot, msg.chat.id, note.message_id)


@mod_action
async def cmd_links(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message; user = update.effective_user
    if msg is None or user is None or msg.chat.type == "private":
        return
    my_rank = await resolve_rank(ctx.bot, msg.chat.id, user.id)
    if await _rank_denied(update, RANK_SENIOR_MOD, my_rank):
        return

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

    target = await _target_user(msg)
    if not target:
        return

    if not await _user_in_chat(ctx.bot, msg.chat.id, target.id):
        return

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
    sent = await send_report(
        ctx.bot, text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=kb,
    )
    if sent is None:
        return
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
    if msg is None or user is None or msg.chat.type == "private":
        return

    parts = (msg.text or "").split()
    if len(parts) != 1:
        return

    t = await _target_user(msg)
    if not t:
        await _no_target(msg); return

    await eph(msg, await _build_profile(ctx, msg.chat.id, t), parse_mode=ParseMode.HTML)


@mod_action
async def cmd_me(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message; user = update.effective_user
    if msg is None or user is None:
        return
    parts = (msg.text or "").split()
    if len(parts) != 1:
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
    "дефварн":      "default_warn_sec",
    "defwarn":      "default_warn_sec",
    "варнвремя":    "default_warn_sec",
    "дефбан":       "default_ban_sec",
    "defban":       "default_ban_sec",
    "банвремя":     "default_ban_sec",
    "триггерварн":  "trig_warn_sec",
    "trigwarnsec":  "trig_warn_sec",
    "триггербан":   "trig_ban_sec",
    "trigbansec":   "trig_ban_sec",
    "ссылкидействие": "link_action",
    "linkaction":   "link_action",
}

BOOL_SETTINGS = {"antispam", "antiraid", "triggers_on", "links_forbidden"}

TIME_SETTINGS = {"flood_mute_sec", "auto_mute_sec", "trig_mute_sec", "default_mute_sec",
                 "antiraid_window", "antiraid_lookback", "ephemeral_delay",
                 "default_warn_sec", "default_ban_sec",
                 "trig_warn_sec", "trig_ban_sec"}

INT_SETTINGS = {"warn_limit", "antiraid_joins", "trig_warn_limit"}

CHOICE_SETTINGS = {
    "warn_action": ("mute", "ban", "kick"),
}

WARN_ACTION_ALIASES = {
    "mute": "mute", "мут": "mute",
    "ban":  "ban",  "бан":  "ban",
    "kick": "kick", "кик":  "kick",
}

WARN_ACTION_DISPLAY = {
    "mute": "мут",
    "ban":  "бан",
    "kick": "кик",
}

LINK_ACTION_ALIASES = {
    "none": "none", "нет": "none", "выкл": "none", "off": "none", "ничего": "none",
    "warn": "warn", "варн": "warn", "пред": "warn",
    "mute": "mute", "мут": "mute",
    "kick": "kick", "кик": "kick",
    "ban":  "ban",  "бан":  "ban",
}

LINK_ACTION_DISPLAY = {
    "none": "ничего (только удаление)",
    "warn": "варн",
    "mute": "мут",
    "kick": "кик",
    "ban":  "бан",
}


def _settings_help() -> str:
    return (
        "<b>Управление настройками</b>\n\n"
        "<b>Время пишется так:</b> <code>10с</code> · <code>30м</code> · <code>1ч</code> · <code>2д</code>\n"
        "(с=секунды, м=минуты, ч=часы, д=дни). <b>0 = вечно/навсегда</b>.\n\n"
        "<b>Тумблеры (вкл / выкл):</b>\n"
        "• <code>настройки антиспам вкл|выкл</code>\n"
        "• <code>настройки антирейд вкл|выкл</code>\n"
        "• <code>настройки триггеры вкл|выкл</code>\n"
        "• <code>настройки ссылки вкл|выкл</code> — глобальный запрет ссылок\n\n"
        "<b>Числа:</b>\n"
        "• <code>настройки варнлимит N</code> — варнов до авто-наказания (команда)\n"
        "• <code>настройки срварн N</code> — отдельный лимит варнов от триггеров\n"
        "• <code>настройки рейдвходы N</code> — сколько входов за окно → алерт\n"
        "• <code>настройки флуд N 10с</code> — N сообщений за время\n\n"
        "<b>Время наказаний:</b>\n"
        "• <code>настройки мутфлуд 30м</code> — мут за флуд\n"
        "• <code>настройки автомут 1ч</code> — длительность авто-мута (в т.ч. при варнах и ссылках)\n"
        "• <code>настройки триггермут 1ч</code> — мут по триггеру\n"
        "• <code>настройки дефмут 10м</code> — мут без указания времени\n"
        "• <code>настройки дефварн 1ч</code> — дефолтный срок варна\n"
        "• <code>настройки дефбан 1д</code> — дефолтный срок бана\n"
        "• <code>настройки триггерварн 1ч</code> — срок варна от триггера\n"
        "• <code>настройки триггербан 1д</code> — срок бана от триггера\n"
        "(для всех этих настроек <b>0 = вечно/навсегда</b>)\n\n"
        "<b>Наказание за ссылки:</b>\n"
        "• <code>настройки ссылкидействие ничего|варн|мут|кик|бан</code>\n"
        "(сроки берутся как у <code>варндействие</code>: дефварн / автомут / дефбан)\n\n"
        "<b>Антирейд окна:</b>\n"
        "• <code>настройки рейдокно 30с</code>\n"
        "• <code>настройки рейдкулдаун 30м</code>\n\n"
        "<b>Служебное:</b>\n"
        "• <code>настройки удаление 60с</code> — через сколько бот удаляет свои сообщения (0 = не удалять)\n\n"
        "<b>Действие при N варнах:</b>\n"
        "• <code>настройки варндействие мут</code> — мут\n"
        "• <code>настройки варндействие бан</code> — бан (по умолчанию)\n"
        "• <code>настройки варндействие кик</code> — кик\n"
    )


@mod_action
async def cmd_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message; user = update.effective_user
    if msg is None or user is None or msg.chat.type == "private":
        return
    my_rank = await resolve_rank(ctx.bot, msg.chat.id, user.id)
    if await _rank_denied(update, RANK_SENIOR_ADMIN, my_rank):
        return

    parts = (msg.text or "").split()

    if len(parts) == 1:
        s = await get_settings(msg.chat.id)
        cur_action = WARN_ACTION_DISPLAY.get(
            str(_s(s, "warn_action", "ban")).lower(), "бан"
        )
        cur_link = LINK_ACTION_DISPLAY.get(
            str(_s(s, "link_action", "none")).lower(), "ничего"
        )

        def _t(key):
            v = int(_s(s, key, 0))
            return fmt_seconds(v) if v > 0 else "вечно"

        cur = (
            f"<b>Текущие настройки</b>\n"
            f"🔹 Антиспам: <b>{'вкл' if s['antispam'] else 'выкл'}</b>\n"
            f"🔹 Антирейд: <b>{'вкл' if s['antiraid'] else 'выкл'}</b> "
            f"(<b>{_s(s, 'antiraid_joins', 10)}</b> входов / <b>{fmt_seconds(int(_s(s, 'antiraid_window', 30)))}</b>, "
            f"кулдаун <b>{fmt_seconds(int(_s(s, 'antiraid_lookback', 1800)))}</b>)\n"
            f"🔹 Триггеры: <b>{'вкл' if s['triggers_on'] else 'выкл'}</b>\n"
            f"🔹 Ссылки запрещены: <b>{'да' if _s(s, 'links_forbidden', 0) else 'нет'}</b>\n"
            f"🔹 Наказание за ссылки: <b>{cur_link}</b>\n"
            f"🔹 Флуд: <b>{s['flood_limit']}</b> сообщ. / <b>{fmt_seconds(int(s['flood_window']))}</b>\n"
            f"🔹 Мут за флуд: <b>{_t('flood_mute_sec')}</b>\n"
            f"🔹 Варнов до авто-наказания: <b>{_s(s, 'warn_limit', 3)}</b>\n"
            f"🔹 Варнов у триггеров: <b>{_s(s, 'trig_warn_limit', 3)}</b>\n"
            f"🔹 Действие при N варнах: <b>{cur_action}</b>\n"
            f"🔹 Авто-мут: <b>{_t('auto_mute_sec')}</b>\n"
            f"🔹 Мут по триггеру: <b>{_t('trig_mute_sec')}</b>\n"
            f"🔹 Мут по умолчанию: <b>{_t('default_mute_sec')}</b>\n"
            f"🔹 Срок варна по умолчанию: <b>{_t('default_warn_sec')}</b>\n"
            f"🔹 Срок бана по умолчанию: <b>{_t('default_ban_sec')}</b>\n"
            f"🔹 Срок варна от триггера: <b>{_t('trig_warn_sec')}</b>\n"
            f"🔹 Срок бана от триггера: <b>{_t('trig_ban_sec')}</b>\n"
            f"🔹 Удаление своих сообщений: <b>{fmt_seconds(int(_s(s, 'ephemeral_delay', 60)))}</b>\n\n"
            + _settings_help()
        )
        await eph(msg, cur, parse_mode=ParseMode.HTML)
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
                  parse_mode=ParseMode.HTML)
        return

    key = SETTING_ALIASES[key_raw]

    if key == "link_action":
        variants = " · ".join(LINK_ACTION_DISPLAY[v] for v in
                              ("none", "warn", "mute", "kick", "ban"))
        if len(parts) < 3:
            await eph(msg,
                      f"❌ Использование: <code>настройки ссылкидействие ничего|варн|мут|кик|бан</code>\n"
                      f"Доступно: <b>{variants}</b>",
                      parse_mode=ParseMode.HTML)
            return
        raw = parts[2].lower()
        val = LINK_ACTION_ALIASES.get(raw)
        if val is None:
            await eph(msg, f"❌ Значение должно быть: <b>{variants}</b>",
                      parse_mode=ParseMode.HTML)
            return
        await set_setting(msg.chat.id, "link_action", val)
        await log_action(ctx.bot, user,
                         f"⚙️ link_action → {LINK_ACTION_DISPLAY[val]}",
                         "—", "", msg.chat.id)
        await eph(msg, f"✅ ссылкидействие → <b>{LINK_ACTION_DISPLAY[val]}</b>",
                  parse_mode=ParseMode.HTML)
        return

    if key in CHOICE_SETTINGS:
        variants = CHOICE_SETTINGS[key]
        pretty = " · ".join(WARN_ACTION_DISPLAY[v] for v in variants)
        if len(parts) < 3:
            await eph(msg,
                      f"❌ Использование: <code>настройки {key_raw} мут|бан|кик</code>",
                      parse_mode=ParseMode.HTML)
            return
        raw = parts[2].lower()
        val = WARN_ACTION_ALIASES.get(raw)
        if val is None or val not in variants:
            await eph(msg, f"❌ Значение должно быть: <b>{pretty}</b>",
                      parse_mode=ParseMode.HTML)
            return
        await set_setting(msg.chat.id, key, val)
        await log_action(ctx.bot, user,
                         f"⚙️ {key} → {WARN_ACTION_DISPLAY[val]}",
                         "—", "", msg.chat.id)
        await eph(msg, f"✅ {key_raw} → <b>{WARN_ACTION_DISPLAY[val]}</b>",
                  parse_mode=ParseMode.HTML)
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
                          f"Формат: 10с / 30м / 1ч / 2д / 0 (вечно)",
                      parse_mode=ParseMode.HTML)
        else:
            await eph(msg, f"❌ Использование: <code>настройки {key_raw} N</code>",
                      parse_mode=ParseMode.HTML)
        return

    if key in TIME_SETTINGS:
        raw = parts[2].strip().lower()
        if raw in ("0", "вечно", "навсегда", "forever", "inf", "infinity"):
            secs = 0
        else:
            secs = parse_setting_time(raw)
            if secs <= 0:
                await eph(msg, "❌ Не распознал время. Пример: <code>10с</code> · <code>30м</code> · <code>1ч</code> · <code>2д</code> · <code>0</code>",
                          parse_mode=ParseMode.HTML)
                return
        await set_setting(msg.chat.id, key, secs)
        pretty = fmt_seconds(secs) if secs > 0 else "вечно"
        await log_action(ctx.bot, user, f"⚙️ {key} → {pretty}", "—", "", msg.chat.id)
        await eph(msg, f"✅ {key_raw} → <b>{pretty}</b>",
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
            BotCommand("activate", "Активация бота в чате"),
        ])
    except Exception as e:
        log.warning("set_my_commands failed: %s", e)


async def _error_handler(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    err = ctx.error
    if err is None:
        return
    name = type(err).__name__
    if name in ("TimedOut", "NetworkError", "ConnectTimeout", "ReadTimeout", "RetryAfter"):
        log.debug("Сеть глюкнула (%s) — продолжаю.", name)
        return
    log.exception("Unhandled error in handler:", exc_info=err)


RU_ALIASES = [
    (r"^активация(?:\s+\S+)?$",            cmd_activate),
    (r"^бан$",                             cmd_ban),
    (r"^бан\s+[0-9]+\s*[a-zA-Zа-яА-Я]$",   cmd_ban),
    (r"^бан\s+[0-9]+\s*[a-zA-Zа-яА-Я]\s+.+$", cmd_ban),
    (r"^бан\s+0$",                         cmd_ban),
    (r"^бан\s+(?:вечно|навсегда)$",        cmd_ban),
    (r"^разбан$",                          cmd_unban),
    (r"^кик$",                             cmd_kick),
    (r"^мут$",                             cmd_mute),
    (r"^мут\s+[0-9]+\s*[a-zA-Zа-яА-Я]$",   cmd_mute),
    (r"^мут\s+0$",                         cmd_mute),
    (r"^мут\s+(?:вечно|навсегда)$",        cmd_mute),
    (r"^размут$",                          cmd_unmute),
    (r"^варн$",                            cmd_warn),
    (r"^варн\s+[0-9]+\s*[a-zA-Zа-яА-Я]$",  cmd_warn),
    (r"^варн\s+0$",                        cmd_warn),
    (r"^варн\s+(?:вечно|навсегда)$",       cmd_warn),
    (r"^анварн$",                          cmd_unwarn),
    (r"^варны$",                           cmd_warns),
    (r"^мод$",                             cmd_mod),
    (r"^репорт(?:\s+.+)?$",                cmd_report),
    (r"^профиль$",                         cmd_profile),
    (r"^я$",                               cmd_me),
    (r"^помощь$",                          cmd_help),
    (r"^ранги$",                           cmd_ranks),
    (r"^роли$",                            cmd_roles),
    (r"^списокролей$",                     cmd_roles),
    (r"^всеранги$",                        cmd_roles),
    (r"^чистка(?:\s+\d+)?$",               cmd_clean),
    (r"^ссылки(?:\s+\S+)?$",               cmd_links),
    (r"^выдатьранг\s+\S+$",                cmd_setrank),
    (r"^снятьранг$",                       cmd_unrank),
    (r"^создатькружок(?:\s+.+)?$",         cmd_create_circle),
    (r"^вступить(?:\s+.+)?$",              cmd_join),
    (r"^выйти(?:\s+.+)?$",                 cmd_leave),
    (r"^инфокружка(?:\s+.+)?$",            cmd_circle_info),
    (r"^удалитькружок(?:\s+.+)?$",         cmd_delete_circle),
    (r"^триггер(?:\s+.+)?$",               cmd_trigger),
    (r"^настройки(?:\s+.+)?$",             cmd_settings),
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
        MessageHandler(G, activation_mw),
        group=-200,
    )

    app.add_handler(
        MessageHandler(filters.ChatType.PRIVATE, private_block_mw),
        group=-100,
    )

    app.add_handler(CommandHandler("activate", cmd_activate, filters=G))

    app.add_handler(MessageHandler(G, pre_remember), group=-4)

    app.add_handler(MessageHandler(G & ~filters.COMMAND, antispam_mw), group=-3)
    app.add_handler(MessageHandler(G & (filters.TEXT | filters.CAPTION), link_guard_mw), group=-2)
    app.add_handler(MessageHandler(G & (filters.TEXT | filters.CAPTION), trigger_mw), group=-1)

    app.add_handler(ChatMemberHandler(antiraid_track, chat_member_types=ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(ChatMemberHandler(on_member_left, chat_member_types=ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(ChatMemberHandler(on_bot_added, chat_member_types=ChatMemberHandler.MY_CHAT_MEMBER))

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
            await app.updater.start_polling(
                allowed_updates=Update.ALL_TYPES,
                drop_pending_updates=False,
            )
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
    _schedule(_punishment_sweeper(app))
    _schedule(_housekeeping())
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
    while True:
        try:
            asyncio.run(_run_bot())
            break
        except KeyboardInterrupt:
            log.info("Bot stopped by user.")
            break
        except SystemExit:
            break
        except Exception as e:
            log.exception("Фатальный краш (не сеть). Рестарт через 15 сек: %s", e)
            try:
                time.sleep(15)
            except KeyboardInterrupt:
                break


if __name__ == "__main__":
    main()
