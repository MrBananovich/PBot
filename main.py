import asyncio
import json
import logging
import random
import sqlite3
import string
from datetime import datetime, timedelta
from pathlib import Path

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.client.default import DefaultBotProperties
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

# ======================
# ⚙️ КОНСТАНТИ
# ======================
TOKEN = "8677587617:AAG7KyLhGfWlbi3MDQeoXN4ClkiiljVIj_Y"
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "anon_bot.db"
LINK_PREFIX = "anon_"
CODE_LENGTH = 10
BOT_USERNAME: str | None = None

# Секретний код для доступу до адмін-панелі
ADMIN_SECRET_CODE = "admin228"  # ← змініть на свій код

# Тимчасовий стан
pending_greeting_input: set[int] = set()
# Адміни які авторизувались: {tg_id}
authorized_admins: set[int] = set()
# Адміни що очікують введення мітки: {tg_id: visitor_db_id}
pending_admin_label: dict[int, int] = {}
# Буфер медіагруп: {media_group_id: {"messages": [...], "task": asyncio.Task}}
media_group_buffer: dict[str, dict] = {}
# Скарги: очікують вибору причини {tg_id: {"visitor_db_id": int, "session_code": str}}
pending_complaint_reason: dict[int, dict] = {}
# Адмін очікує введення строку таймауту: {tg_id: complaint_id}
pending_admin_timeout: dict[int, int] = {}
# Адмін пише новину для розсилки: {tg_id: True}
pending_admin_news: set[int] = set()
# Тимчасовий текст новини до підтвердження: {tg_id: text}
pending_admin_news_text: dict[int, str] = {}
# Адмін вводить нову репутацію: {tg_id: user_db_id}
pending_admin_rep_set: dict[int, int] = {}
# Користувачі що активували режим відповіді через кнопку: {tg_id}
active_reply_mode: set[int] = set()

# Режим техобслуговування (True = бот закритий для звичайних користувачів)
maintenance_mode: bool = False

# Причини скарг
COMPLAINT_REASONS: dict[str, str] = {
    "spam":       "🗞 Спам",
    "insult":     "🤬 Образи / хамство",
    "threat":     "⚠️ Погрози",
    "adult":      "🔞 Небажаний контент",
    "fraud":      "💸 Шахрайство",
    "other":      "📝 Інше",
}

# ======================
# ⚙️ ТИПИ КОНТЕНТУ
# ======================
CONTENT_TYPES: dict[str, dict] = {
    "text":       {"emoji": "✍️", "label": "Текст"},
    "photo":      {"emoji": "🖼", "label": "Фото"},
    "video":      {"emoji": "🎬", "label": "Відео"},
    "audio":      {"emoji": "🎵", "label": "Музика"},
    "voice":      {"emoji": "🎙", "label": "Голосові"},
    "video_note": {"emoji": "⭕", "label": "Кружки"},
    "document":   {"emoji": "📎", "label": "Файли"},
    "sticker":    {"emoji": "😄", "label": "Стікери"},
    "animation":  {"emoji": "🌀", "label": "GIF"},
    "location":   {"emoji": "📍", "label": "Локації"},
    "contact":    {"emoji": "👤", "label": "Контакти"},
}
DEFAULT_SETTINGS = {k: 1 for k in CONTENT_TYPES}

logging.basicConfig(level=logging.INFO)

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()

# ======================
# ⚙️ РОБОТА З БД
# ======================

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tg_id INTEGER UNIQUE,
                username TEXT,
                anon_code TEXT UNIQUE,
                rep INTEGER DEFAULT 0,
                sent_count INTEGER DEFAULT 0,
                received_count INTEGER DEFAULT 0,
                current_session_code TEXT DEFAULT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                visitor_id INTEGER,
                target_id INTEGER,
                session_code TEXT UNIQUE,
                active INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_activity TIMESTAMP,
                FOREIGN KEY(visitor_id) REFERENCES users(id),
                FOREIGN KEY(target_id) REFERENCES users(id)
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_replies (
                target_id INTEGER PRIMARY KEY,
                session_code TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                target_msg_id INTEGER DEFAULT NULL,
                visitor_msg_id INTEGER DEFAULT NULL,
                FOREIGN KEY(target_id) REFERENCES users(id)
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS votes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                voter_id INTEGER,
                subject_id INTEGER,
                value INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(voter_id, subject_id)
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER PRIMARY KEY,
                allow_text INTEGER DEFAULT 1,
                allow_photo INTEGER DEFAULT 1,
                allow_video INTEGER DEFAULT 1,
                allow_audio INTEGER DEFAULT 1,
                allow_voice INTEGER DEFAULT 1,
                allow_video_note INTEGER DEFAULT 1,
                allow_document INTEGER DEFAULT 1,
                allow_sticker INTEGER DEFAULT 1,
                allow_animation INTEGER DEFAULT 1,
                allow_location INTEGER DEFAULT 1,
                allow_contact INTEGER DEFAULT 1,
                greeting TEXT DEFAULT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS blocks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                blocker_id INTEGER,
                blocked_visitor_id INTEGER,
                session_code TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(blocker_id, blocked_visitor_id),
                FOREIGN KEY(blocker_id) REFERENCES users(id),
                FOREIGN KEY(blocked_visitor_id) REFERENCES users(id)
            )
            """
        )
        # НОВА ТАБЛИЦЯ: адміністративні мітки на аноніми
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS admin_labels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_db_id INTEGER UNIQUE,
                label TEXT,
                set_by_tg_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_db_id) REFERENCES users(id)
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS complaints (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reporter_id INTEGER,
                accused_id INTEGER,
                session_code TEXT,
                reason TEXT,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                resolved_at TIMESTAMP,
                resolved_by INTEGER,
                FOREIGN KEY(reporter_id) REFERENCES users(id),
                FOREIGN KEY(accused_id) REFERENCES users(id)
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS bans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_db_id INTEGER UNIQUE,
                ban_type TEXT,
                reason TEXT,
                banned_until TIMESTAMP,
                banned_by INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_db_id) REFERENCES users(id)
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS admins (
                tg_id INTEGER PRIMARY KEY,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS admin_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_tg_id INTEGER,
                action TEXT,
                target_user_id INTEGER DEFAULT NULL,
                details TEXT DEFAULT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        # НОВА ТАБЛИЦЯ: зв'язок оригінал-повідомлення з його копією у іншого учасника (для редагування)
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS message_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                author_chat_id INTEGER,
                author_msg_id INTEGER,
                recipient_chat_id INTEGER,
                recipient_msg_id INTEGER,
                content_type TEXT,
                session_code TEXT,
                reply_markup_json TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(author_chat_id, author_msg_id)
            )
            """
        )
        conn.commit()
    finally:
        conn.close()

    # Міграції для старих БД
    conn = get_db_connection()
    try:
        cursor = conn.execute("PRAGMA table_info(users)")
        cols = [r[1] for r in cursor.fetchall()]
        if "rep" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN rep INTEGER DEFAULT 0")
        if "sent_count" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN sent_count INTEGER DEFAULT 0")
        if "received_count" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN received_count INTEGER DEFAULT 0")
        if "current_session_code" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN current_session_code TEXT DEFAULT NULL")
        conn.commit()
    finally:
        conn.close()

    conn = get_db_connection()
    try:
        cursor = conn.execute("PRAGMA table_info(user_settings)")
        existing_cols = {r[1] for r in cursor.fetchall()}
        needed_cols = [f"allow_{ct}" for ct in CONTENT_TYPES]
        for col in needed_cols:
            if col not in existing_cols:
                conn.execute(f"ALTER TABLE user_settings ADD COLUMN {col} INTEGER DEFAULT 1")
        if "greeting" not in existing_cols:
            conn.execute("ALTER TABLE user_settings ADD COLUMN greeting TEXT DEFAULT NULL")
        conn.commit()
    finally:
        conn.close()

    # Міграція: додаємо колонки для reply-свайпу в pending_replies
    conn = get_db_connection()
    try:
        cursor = conn.execute("PRAGMA table_info(pending_replies)")
        pr_cols = {r[1] for r in cursor.fetchall()}
        if "target_msg_id" not in pr_cols:
            conn.execute("ALTER TABLE pending_replies ADD COLUMN target_msg_id INTEGER DEFAULT NULL")
        if "visitor_msg_id" not in pr_cols:
            conn.execute("ALTER TABLE pending_replies ADD COLUMN visitor_msg_id INTEGER DEFAULT NULL")
        conn.commit()
    finally:
        conn.close()

    # Міграція: ID останньої відповіді що надійшла відвідувачу (для свайпу у зворотній бік)
    conn = get_db_connection()
    try:
        cursor = conn.execute("PRAGMA table_info(sessions)")
        s_cols = {r[1] for r in cursor.fetchall()}
        if "last_visitor_inbox_msg_id" not in s_cols:
            conn.execute("ALTER TABLE sessions ADD COLUMN last_visitor_inbox_msg_id INTEGER DEFAULT NULL")
        conn.commit()
    finally:
        conn.close()


def generate_code() -> str:
    alphabet = string.ascii_letters + string.digits
    return LINK_PREFIX + "".join(random.choices(alphabet, k=CODE_LENGTH))


def find_user_by_tg_id(tg_id: int):
    conn = get_db_connection()
    try:
        return conn.execute("SELECT * FROM users WHERE tg_id = ?", (tg_id,)).fetchone()
    finally:
        conn.close()


def find_user_by_code(code: str):
    conn = get_db_connection()
    try:
        return conn.execute("SELECT * FROM users WHERE anon_code = ?", (code,)).fetchone()
    finally:
        conn.close()


def find_user_by_id(user_id: int):
    conn = get_db_connection()
    try:
        return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    finally:
        conn.close()


def create_or_update_user(tg_id: int, username: str | None):
    existing = find_user_by_tg_id(tg_id)
    conn = get_db_connection()
    try:
        if existing:
            conn.execute(
                "UPDATE users SET username = ? WHERE tg_id = ?",
                (username or existing["username"], tg_id),
            )
            conn.commit()
            return find_user_by_tg_id(tg_id)

        anon_code = generate_code()
        conn.execute(
            "INSERT INTO users (tg_id, username, anon_code) VALUES (?, ?, ?)",
            (tg_id, username, anon_code),
        )
        conn.commit()
        return find_user_by_tg_id(tg_id)
    finally:
        conn.close()


def reset_user_code(tg_id: int) -> str:
    anon_code = generate_code()
    conn = get_db_connection()
    try:
        conn.execute("UPDATE users SET anon_code = ? WHERE tg_id = ?", (anon_code, tg_id))
        conn.execute(
            "UPDATE sessions SET active = 0 WHERE target_id = (SELECT id FROM users WHERE tg_id = ?)",
            (tg_id,),
        )
        user = conn.execute("SELECT id FROM users WHERE tg_id = ?", (tg_id,)).fetchone()
        if user:
            conn.execute("DELETE FROM pending_replies WHERE target_id = ?", (user["id"],))
        conn.commit()
        return anon_code
    finally:
        conn.close()


def close_visitor_session(visitor_db_id: int) -> bool:
    """Закриває УСІ активні сесії відвідувача (а не лише одну — інакше старі
    'сирітські' сесії лишались активними і могли підхоплюватись помилково)
    та очищає його поточний орієнтир сесії."""
    conn = get_db_connection()
    try:
        cursor = conn.execute(
            "UPDATE sessions SET active = 0 WHERE visitor_id = ? AND active = 1",
            (visitor_db_id,),
        )
        had_active = cursor.rowcount > 0
        conn.execute(
            "UPDATE users SET current_session_code = NULL WHERE id = ?",
            (visitor_db_id,),
        )
        conn.commit()
        return had_active
    finally:
        conn.close()


def get_or_create_session(visitor_id: int, target_id: int):
    conn = get_db_connection()
    try:
        session = conn.execute(
            "SELECT * FROM sessions WHERE visitor_id = ? AND target_id = ? AND active = 1",
            (visitor_id, target_id),
        ).fetchone()
        if session:
            conn.execute(
                "UPDATE sessions SET last_activity = ? WHERE id = ?",
                (datetime.utcnow(), session["id"]),
            )
            conn.commit()
            session_code = session["session_code"]
        else:
            session_code = generate_code()
            conn.execute(
                "INSERT INTO sessions (visitor_id, target_id, session_code, last_activity) VALUES (?, ?, ?, ?)",
                (visitor_id, target_id, session_code, datetime.utcnow()),
            )
            conn.commit()

        # Явно фіксуємо: саме ця сесія тепер є поточним діалогом відвідувача
        conn.execute(
            "UPDATE users SET current_session_code = ? WHERE id = ?",
            (session_code, visitor_id),
        )
        conn.commit()
        return conn.execute(
            "SELECT * FROM sessions WHERE session_code = ?", (session_code,)
        ).fetchone()
    finally:
        conn.close()


def find_active_session_by_visitor(visitor_id: int):
    """Повертає сесію, яку відвідувач явно відкрив останньою (current_session_code),
    а не 'якусь випадкову активну' — це усуває плутанину адресатів, коли
    у відвідувача залишались паралельні активні сесії з різними людьми."""
    conn = get_db_connection()
    try:
        user = conn.execute(
            "SELECT current_session_code FROM users WHERE id = ?", (visitor_id,)
        ).fetchone()
        if not user or not user["current_session_code"]:
            return None
        session = conn.execute(
            "SELECT * FROM sessions WHERE session_code = ? AND visitor_id = ? AND active = 1",
            (user["current_session_code"], visitor_id),
        ).fetchone()
        return session
    finally:
        conn.close()


def find_session_by_code(session_code: str):
    """Знаходить сесію за кодом — як активну так і закриту (для відповіді)."""
    conn = get_db_connection()
    try:
        return conn.execute(
            "SELECT * FROM sessions WHERE session_code = ?", (session_code,)
        ).fetchone()
    finally:
        conn.close()


def update_visitor_inbox_msg_id(session_code: str, msg_id: int):
    """Зберігає ID повідомлення-відповіді що надійшла відвідувачу (для свайп-reply)."""
    conn = get_db_connection()
    try:
        conn.execute(
            "UPDATE sessions SET last_visitor_inbox_msg_id = ? WHERE session_code = ?",
            (msg_id, session_code),
        )
        conn.commit()
    finally:
        conn.close()


def set_reply_pending(target_id: int, session_code: str,
                      target_msg_id: int | None = None, visitor_msg_id: int | None = None):
    conn = get_db_connection()
    try:
        conn.execute(
            "REPLACE INTO pending_replies (target_id, session_code, created_at, target_msg_id, visitor_msg_id) VALUES (?, ?, ?, ?, ?)",
            (target_id, session_code, datetime.utcnow(), target_msg_id, visitor_msg_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_reply_pending(target_id: int):
    conn = get_db_connection()
    try:
        return conn.execute(
            "SELECT * FROM pending_replies WHERE target_id = ?", (target_id,)
        ).fetchone()
    finally:
        conn.close()


def clear_reply_pending(target_id: int):
    conn = get_db_connection()
    try:
        conn.execute("DELETE FROM pending_replies WHERE target_id = ?", (target_id,))
        conn.commit()
    finally:
        conn.close()


# ======================
# ⚙️ ЗВ'ЯЗОК ПОВІДОМЛЕНЬ (ДЛЯ РЕДАГУВАННЯ)
# ======================

def save_message_link(author_chat_id: int, author_msg_id: int,
                       recipient_chat_id: int, recipient_msg_id: int,
                       content_type: str, session_code: str | None = None,
                       reply_markup: InlineKeyboardMarkup | None = None):
    """Зберігає зв'язок: оригінальне повідомлення відправника -> його копія у отримувача.
    Потрібно, щоб при редагуванні оригіналу можна було відредагувати й копію
    (і відновити ту саму inline-клавіатуру, бо editMessageText/Caption її інакше прибирає)."""
    markup_json = None
    if reply_markup is not None:
        try:
            markup_json = json.dumps(reply_markup.model_dump(exclude_none=True))
        except Exception:
            markup_json = None
    conn = get_db_connection()
    try:
        conn.execute(
            """REPLACE INTO message_links
               (author_chat_id, author_msg_id, recipient_chat_id, recipient_msg_id,
                content_type, session_code, reply_markup_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (author_chat_id, author_msg_id, recipient_chat_id, recipient_msg_id,
             content_type, session_code, markup_json, datetime.utcnow()),
        )
        conn.commit()
    finally:
        conn.close()


def find_message_link(author_chat_id: int, author_msg_id: int):
    """Знаходить копію повідомлення за оригіналом (chat_id, message_id) відправника."""
    conn = get_db_connection()
    try:
        return conn.execute(
            "SELECT * FROM message_links WHERE author_chat_id = ? AND author_msg_id = ?",
            (author_chat_id, author_msg_id),
        ).fetchone()
    finally:
        conn.close()


def add_vote(voter_id: int, subject_id: int, value: int) -> int:
    if value not in (1, -1):
        return 0

    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT value FROM votes WHERE voter_id = ? AND subject_id = ?",
            (voter_id, subject_id),
        ).fetchone()

        if row:
            if row["value"] == value:
                return conn.execute(
                    "SELECT rep FROM users WHERE id = ?", (subject_id,)
                ).fetchone()["rep"]
            conn.execute(
                "UPDATE votes SET value = ?, created_at = ? WHERE voter_id = ? AND subject_id = ?",
                (value, datetime.utcnow(), voter_id, subject_id),
            )
            conn.execute(
                "UPDATE users SET rep = rep + ? WHERE id = ?", (2 * value, subject_id)
            )
        else:
            conn.execute(
                "INSERT INTO votes (voter_id, subject_id, value, created_at) VALUES (?, ?, ?, ?)",
                (voter_id, subject_id, value, datetime.utcnow()),
            )
            conn.execute(
                "UPDATE users SET rep = rep + ? WHERE id = ?", (value, subject_id)
            )

        conn.commit()
        return conn.execute(
            "SELECT rep FROM users WHERE id = ?", (subject_id,)
        ).fetchone()["rep"]
    finally:
        conn.close()


def get_user_rep_by_id(user_id: int) -> int:
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT rep FROM users WHERE id = ?", (user_id,)).fetchone()
        return row["rep"] if row else 0
    finally:
        conn.close()


def increment_sent_count(user_id: int, by: int = 1) -> None:
    """Збільшує лічильник надісланих повідомлень користувача."""
    conn = get_db_connection()
    try:
        conn.execute(
            "UPDATE users SET sent_count = sent_count + ? WHERE id = ?", (by, user_id)
        )
        conn.commit()
    finally:
        conn.close()


def increment_received_count(user_id: int, by: int = 1) -> None:
    """Збільшує лічильник отриманих повідомлень користувача."""
    conn = get_db_connection()
    try:
        conn.execute(
            "UPDATE users SET received_count = received_count + ? WHERE id = ?", (by, user_id)
        )
        conn.commit()
    finally:
        conn.close()


def get_user_message_stats(user_id: int) -> dict:
    """Повертає кількість надісланих і отриманих повідомлень користувача."""
    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT sent_count, received_count FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if not row:
            return {"sent": 0, "received": 0}
        return {"sent": row["sent_count"] or 0, "received": row["received_count"] or 0}
    finally:
        conn.close()


def get_user_rep_stats(user_id: int) -> dict:
    """Повертає детальну статистику репутації користувача."""
    conn = get_db_connection()
    try:
        rep_row = conn.execute("SELECT rep FROM users WHERE id = ?", (user_id,)).fetchone()
        rep = rep_row["rep"] if rep_row else 0
        total = conn.execute(
            "SELECT COUNT(*) as c FROM votes WHERE subject_id = ?", (user_id,)
        ).fetchone()["c"]
        positive = conn.execute(
            "SELECT COUNT(*) as c FROM votes WHERE subject_id = ? AND value = 1", (user_id,)
        ).fetchone()["c"]
        negative = conn.execute(
            "SELECT COUNT(*) as c FROM votes WHERE subject_id = ? AND value = -1", (user_id,)
        ).fetchone()["c"]
        pct = round(positive / total * 100) if total > 0 else None
        return {"rep": rep, "total": total, "positive": positive, "negative": negative, "pct": pct}
    finally:
        conn.close()


def get_rep_level(rep: int) -> tuple[str, str]:
    if rep <= -20:
        return "💀", "Чорна мітка"
    elif rep <= -10:
        return "😈", "Токсична особа"
    elif rep <= -4:
        return "😤", "Підозріла особа"
    elif rep <= -1:
        return "😒", "Не дуже надійна"
    elif rep <= 2:
        return "😐", "Новачок"
    elif rep <= 7:
        return "🙂", "Нейтральна"
    elif rep <= 14:
        return "🤝", "Довірена"
    elif rep <= 24:
        return "⭐", "Авторитет"
    elif rep <= 49:
        return "🌟", "Поважна особа"
    else:
        return "👑", "Легенда"


def get_rep_bar(rep: int) -> str:
    """Динамічний прогрес-бар 10 сегментів: від -50 до +50."""
    clamped = max(-50, min(50, rep))
    # Нормалізуємо до 0..10
    filled = round((clamped + 50) / 10)
    filled = max(0, min(10, filled))
    return "█" * filled + "░" * (10 - filled)


# ======================
# ⚙️ АДМІНІСТРАТИВНІ МІТКИ
# ======================

def get_admin_label(user_db_id: int) -> str | None:
    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT label FROM admin_labels WHERE user_db_id = ?", (user_db_id,)
        ).fetchone()
        return row["label"] if row else None
    finally:
        conn.close()


def set_admin_label(user_db_id: int, label: str | None, admin_tg_id: int):
    conn = get_db_connection()
    try:
        if label:
            conn.execute(
                """INSERT INTO admin_labels (user_db_id, label, set_by_tg_id, created_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(user_db_id) DO UPDATE SET label=excluded.label,
                   set_by_tg_id=excluded.set_by_tg_id, created_at=excluded.created_at""",
                (user_db_id, label, admin_tg_id, datetime.utcnow()),
            )
        else:
            conn.execute("DELETE FROM admin_labels WHERE user_db_id = ?", (user_db_id,))
        conn.commit()
    finally:
        conn.close()


def get_all_admin_labels() -> list:
    conn = get_db_connection()
    try:
        return conn.execute(
            """SELECT al.user_db_id, al.label, al.created_at, u.tg_id, u.username
               FROM admin_labels al
               JOIN users u ON u.id = al.user_db_id
               ORDER BY al.created_at DESC"""
        ).fetchall()
    finally:
        conn.close()


def get_user_stats() -> dict:
    conn = get_db_connection()
    try:
        total = conn.execute("SELECT COUNT(*) as c FROM users").fetchone()["c"]
        sessions_total = conn.execute("SELECT COUNT(*) as c FROM sessions").fetchone()["c"]
        labeled = conn.execute("SELECT COUNT(*) as c FROM admin_labels").fetchone()["c"]
        pending_complaints = conn.execute(
            "SELECT COUNT(*) as c FROM complaints WHERE status = 'pending'"
        ).fetchone()["c"]
        total_sent = conn.execute(
            "SELECT COALESCE(SUM(sent_count), 0) as c FROM users"
        ).fetchone()["c"]
        total_received = conn.execute(
            "SELECT COALESCE(SUM(received_count), 0) as c FROM users"
        ).fetchone()["c"]
        return {"users": total, "sessions": sessions_total, "labeled": labeled,
                "pending_complaints": pending_complaints,
                "total_sent": total_sent, "total_received": total_received}
    finally:
        conn.close()


def get_all_user_tg_ids() -> list[int]:
    conn = get_db_connection()
    try:
        rows = conn.execute("SELECT tg_id FROM users").fetchall()
        return [r["tg_id"] for r in rows]
    finally:
        conn.close()


# ======================
# ⚙️ АДМІНИ (PERSISTENT)
# ======================

def db_add_admin(tg_id: int):
    conn = get_db_connection()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO admins (tg_id) VALUES (?)", (tg_id,)
        )
        conn.commit()
    finally:
        conn.close()


def db_remove_admin(tg_id: int):
    conn = get_db_connection()
    try:
        conn.execute("DELETE FROM admins WHERE tg_id = ?", (tg_id,))
        conn.commit()
    finally:
        conn.close()


def db_get_all_admin_tg_ids() -> list[int]:
    conn = get_db_connection()
    try:
        rows = conn.execute("SELECT tg_id FROM admins").fetchall()
        return [r["tg_id"] for r in rows]
    finally:
        conn.close()


# ======================
# ⚙️ ЖУРНАЛ ДІЙ АДМІНІВ
# ======================

def admin_log(admin_tg_id: int, action: str, target_user_id: int | None = None, details: str | None = None):
    conn = get_db_connection()
    try:
        conn.execute(
            "INSERT INTO admin_log (admin_tg_id, action, target_user_id, details, created_at) VALUES (?,?,?,?,?)",
            (admin_tg_id, action, target_user_id, details, datetime.utcnow()),
        )
        conn.commit()
    finally:
        conn.close()


def get_admin_log(limit: int = 30) -> list:
    conn = get_db_connection()
    try:
        return conn.execute(
            """SELECT al.*, u.username, u.tg_id as target_tg
               FROM admin_log al
               LEFT JOIN users u ON u.id = al.target_user_id
               ORDER BY al.created_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    finally:
        conn.close()


# ======================
# ⚙️ РЕПУТАЦІЯ (АДМІН)
# ======================

def admin_set_rep(user_db_id: int, new_rep: int):
    """Встановлює репутацію вручну, очищає всі голоси."""
    conn = get_db_connection()
    try:
        conn.execute("UPDATE users SET rep = ? WHERE id = ?", (new_rep, user_db_id))
        conn.execute("DELETE FROM votes WHERE subject_id = ?", (user_db_id,))
        conn.commit()
    finally:
        conn.close()


def admin_add_rep(user_db_id: int, delta: int) -> int:
    """Додає/віднімає бали репутації без зміни голосів. Повертає нове значення."""
    conn = get_db_connection()
    try:
        conn.execute("UPDATE users SET rep = rep + ? WHERE id = ?", (delta, user_db_id))
        conn.commit()
        return conn.execute("SELECT rep FROM users WHERE id = ?", (user_db_id,)).fetchone()["rep"]
    finally:
        conn.close()


# ======================
# ⚙️ СПИСОК ЗАБАНЕНИХ
# ======================

def get_all_bans() -> list:
    conn = get_db_connection()
    try:
        return conn.execute(
            """SELECT b.*, u.tg_id, u.username, u.rep
               FROM bans b
               JOIN users u ON u.id = b.user_db_id
               ORDER BY b.created_at DESC"""
        ).fetchall()
    finally:
        conn.close()

def create_complaint(reporter_id: int, accused_id: int, session_code: str, reason: str) -> int:
    conn = get_db_connection()
    try:
        # Одна активна скарга від того самого репортера на того самого за тією ж сесією
        existing = conn.execute(
            "SELECT id FROM complaints WHERE reporter_id=? AND accused_id=? AND session_code=? AND status='pending'",
            (reporter_id, accused_id, session_code),
        ).fetchone()
        if existing:
            return existing["id"]
        conn.execute(
            "INSERT INTO complaints (reporter_id, accused_id, session_code, reason) VALUES (?,?,?,?)",
            (reporter_id, accused_id, session_code, reason),
        )
        conn.commit()
        return conn.execute("SELECT last_insert_rowid() as id").fetchone()["id"]
    finally:
        conn.close()


def get_pending_complaints() -> list:
    conn = get_db_connection()
    try:
        return conn.execute(
            """SELECT c.*, 
               u_rep.tg_id as reporter_tg, u_rep.username as reporter_username,
               u_acc.tg_id as accused_tg, u_acc.username as accused_username
               FROM complaints c
               JOIN users u_rep ON u_rep.id = c.reporter_id
               JOIN users u_acc ON u_acc.id = c.accused_id
               WHERE c.status = 'pending'
               ORDER BY c.created_at ASC"""
        ).fetchall()
    finally:
        conn.close()


def get_complaint_by_id(complaint_id: int):
    conn = get_db_connection()
    try:
        return conn.execute(
            """SELECT c.*,
               u_rep.tg_id as reporter_tg, u_rep.username as reporter_username,
               u_acc.tg_id as accused_tg, u_acc.username as accused_username
               FROM complaints c
               JOIN users u_rep ON u_rep.id = c.reporter_id
               JOIN users u_acc ON u_acc.id = c.accused_id
               WHERE c.id = ?""",
            (complaint_id,),
        ).fetchone()
    finally:
        conn.close()


def resolve_complaint(complaint_id: int, status: str, admin_tg_id: int):
    """status: 'banned' | 'timeout' | 'dismissed'"""
    conn = get_db_connection()
    try:
        conn.execute(
            "UPDATE complaints SET status=?, resolved_at=?, resolved_by=? WHERE id=?",
            (status, datetime.utcnow(), admin_tg_id, complaint_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_complaints_count_pending() -> int:
    conn = get_db_connection()
    try:
        return conn.execute(
            "SELECT COUNT(*) as c FROM complaints WHERE status='pending'"
        ).fetchone()["c"]
    finally:
        conn.close()


# ======================
# ⚙️ БАНИ
# ======================

def ban_user(user_db_id: int, ban_type: str, reason: str, admin_tg_id: int,
             banned_until: datetime | None = None):
    """ban_type: 'permanent' | 'timeout'"""
    conn = get_db_connection()
    try:
        conn.execute(
            """INSERT INTO bans (user_db_id, ban_type, reason, banned_until, banned_by)
               VALUES (?,?,?,?,?)
               ON CONFLICT(user_db_id) DO UPDATE SET
               ban_type=excluded.ban_type, reason=excluded.reason,
               banned_until=excluded.banned_until, banned_by=excluded.banned_by,
               created_at=excluded.created_at""",
            (user_db_id, ban_type, reason, banned_until, admin_tg_id),
        )
        conn.commit()
    finally:
        conn.close()


def unban_user(user_db_id: int):
    conn = get_db_connection()
    try:
        conn.execute("DELETE FROM bans WHERE user_db_id = ?", (user_db_id,))
        conn.commit()
    finally:
        conn.close()


def get_ban(user_db_id: int):
    conn = get_db_connection()
    try:
        return conn.execute(
            "SELECT * FROM bans WHERE user_db_id = ?", (user_db_id,)
        ).fetchone()
    finally:
        conn.close()


def is_banned(user_db_id: int) -> tuple[bool, str | None]:
    """Повертає (True, опис) якщо забанений, (False, None) якщо ні."""
    ban = get_ban(user_db_id)
    if not ban:
        return False, None
    if ban["ban_type"] == "permanent":
        return True, "⛔ Вас <b>назавжди заблоковано</b> в цьому боті за порушення правил."
    if ban["banned_until"]:
        until = datetime.fromisoformat(str(ban["banned_until"]))
        if datetime.utcnow() < until:
            until_local = until.strftime("%d.%m.%Y %H:%M UTC")
            return True, f"⏱ Вас тимчасово обмежено до <b>{until_local}</b>."
        # Таймаут минув — автоматично знімаємо
        unban_user(user_db_id)
    return False, None


def parse_duration(text: str) -> timedelta | None:
    """Парсить '2h', '30m', '1d', '7d' тощо. Повертає timedelta або None."""
    text = text.strip().lower()
    import re
    m = re.fullmatch(r"(\d+)\s*(m|h|d)", text)
    if not m:
        return None
    val = int(m.group(1))
    unit = m.group(2)
    if unit == "m":
        return timedelta(minutes=val)
    if unit == "h":
        return timedelta(hours=val)
    if unit == "d":
        return timedelta(days=val)
    return None


def build_admin_label_line(visitor_db_id: int) -> str:
    """Повертає рядок з міткою адміна або порожній рядок."""
    label = get_admin_label(visitor_db_id)
    if label:
        return f"\n⚠️ <b>Адмін-мітка:</b> {label}"
    return ""


# ======================
# ⚙️ НАЛАШТУВАННЯ КОНТЕНТУ
# ======================

def get_user_settings(user_db_id: int) -> dict[str, int]:
    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT * FROM user_settings WHERE user_id = ?", (user_db_id,)
        ).fetchone()
        if row:
            result = {}
            for ct in CONTENT_TYPES:
                try:
                    result[ct] = row[f"allow_{ct}"]
                except IndexError:
                    result[ct] = 1
            return result
        cols = ", ".join(f"allow_{ct}" for ct in CONTENT_TYPES)
        vals = ", ".join("1" for _ in CONTENT_TYPES)
        conn.execute(
            f"INSERT INTO user_settings (user_id, {cols}) VALUES (?, {vals})",
            (user_db_id,),
        )
        conn.commit()
        return dict(DEFAULT_SETTINGS)
    finally:
        conn.close()


def toggle_user_setting(user_db_id: int, content_type: str) -> int:
    if content_type not in CONTENT_TYPES:
        return -1
    get_user_settings(user_db_id)
    conn = get_db_connection()
    try:
        row = conn.execute(
            f"SELECT allow_{content_type} FROM user_settings WHERE user_id = ?",
            (user_db_id,),
        ).fetchone()
        new_val = 0 if row[f"allow_{content_type}"] else 1
        conn.execute(
            f"UPDATE user_settings SET allow_{content_type} = ? WHERE user_id = ?",
            (new_val, user_db_id),
        )
        conn.commit()
        return new_val
    finally:
        conn.close()


def is_content_allowed(target_db_id: int, content_type: str) -> bool:
    settings = get_user_settings(target_db_id)
    return bool(settings.get(content_type, 1))


def get_content_type_of_message(message: types.Message) -> str | None:
    if message.text:        return "text"
    if message.photo:       return "photo"
    if message.video:       return "video"
    if message.audio:       return "audio"
    if message.voice:       return "voice"
    if message.video_note:  return "video_note"
    if message.document:    return "document"
    if message.sticker:     return "sticker"
    if message.animation:   return "animation"
    if message.location:    return "location"
    if message.contact:     return "contact"
    return None


def build_restrictions_text(settings: dict[str, int]) -> str:
    blocked = [
        f"{CONTENT_TYPES[ct]['emoji']} {CONTENT_TYPES[ct]['label']}"
        for ct in CONTENT_TYPES
        if not settings.get(ct, 1)
    ]
    if not blocked:
        return ""
    lines = "\n".join(f"  • {b}" for b in blocked)
    return f"\n\n⛔ <b>Цей користувач не приймає:</b>\n{lines}"


# ======================
# ⚙️ ПРИВІТАННЯ
# ======================

def get_user_greeting(user_db_id: int) -> str | None:
    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT greeting FROM user_settings WHERE user_id = ?", (user_db_id,)
        ).fetchone()
        if row:
            try:
                return row["greeting"]
            except IndexError:
                return None
        return None
    finally:
        conn.close()


def set_user_greeting(user_db_id: int, text: str | None):
    get_user_settings(user_db_id)
    conn = get_db_connection()
    try:
        conn.execute(
            "UPDATE user_settings SET greeting = ? WHERE user_id = ?",
            (text, user_db_id),
        )
        conn.commit()
    finally:
        conn.close()


def build_greeting_text(target_db_id: int) -> str:
    custom = get_user_greeting(target_db_id)
    custom_line = f"💬 {custom}\n" if custom else ""
    return (
        "┌─────────────────┐\n"
        "   ✅ Анонімний чат\n"
        f"{custom_line}"
        "  Надішліть повідомлення\n"
        "  або файл — я передам\n"
        "  його анонімно 🤫\n\n"
        "  /stop — завершити чат\n"
        "└─────────────────┘"
    )


# ======================
# ⚙️ БЛОКУВАННЯ
# ======================

def block_visitor(blocker_id: int, blocked_visitor_id: int, session_code: str):
    conn = get_db_connection()
    try:
        conn.execute(
            """INSERT OR REPLACE INTO blocks (blocker_id, blocked_visitor_id, session_code, created_at)
               VALUES (?, ?, ?, ?)""",
            (blocker_id, blocked_visitor_id, session_code, datetime.utcnow()),
        )
        conn.execute(
            "UPDATE sessions SET active = 0 WHERE visitor_id = ? AND target_id = ?",
            (blocked_visitor_id, blocker_id),
        )
        conn.commit()
    finally:
        conn.close()


def unblock_visitor(blocker_id: int, blocked_visitor_id: int):
    conn = get_db_connection()
    try:
        conn.execute(
            "DELETE FROM blocks WHERE blocker_id = ? AND blocked_visitor_id = ?",
            (blocker_id, blocked_visitor_id),
        )
        conn.commit()
    finally:
        conn.close()


def unblock_all(blocker_id: int):
    conn = get_db_connection()
    try:
        conn.execute("DELETE FROM blocks WHERE blocker_id = ?", (blocker_id,))
        conn.commit()
    finally:
        conn.close()


def is_blocked(blocker_id: int, visitor_id: int) -> bool:
    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT id FROM blocks WHERE blocker_id = ? AND blocked_visitor_id = ?",
            (blocker_id, visitor_id),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def get_blocked_list(blocker_id: int) -> list:
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT blocked_visitor_id, created_at FROM blocks WHERE blocker_id = ? ORDER BY created_at DESC",
            (blocker_id,),
        ).fetchall()
        return list(rows)
    finally:
        conn.close()


# ======================
# ⚙️ КЛАВІАТУРИ
# ======================

def build_main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 Отримати своє посилання", callback_data="show_link")],
        [InlineKeyboardButton(text="🔄 Скинути посилання", callback_data="reset_link")],
        [InlineKeyboardButton(text="📊 Мій рейтинг", callback_data="show_rep")],
        [InlineKeyboardButton(text="⚙️ Налаштування прийому", callback_data="settings")],
        [InlineKeyboardButton(text="❓ Інструкція", callback_data="help")],
    ])


def build_message_markup(session_code: str, visitor_db_id: int) -> InlineKeyboardMarkup:
    rep = get_user_rep_by_id(visitor_db_id)
    emoji, level = get_rep_level(rep)
    bar = get_rep_bar(rep)
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"{emoji} {level}  [{bar}]  {rep:+d}",
            callback_data="rep_info",
        )],
        [InlineKeyboardButton(text="↩️ Відповісти анонімно", callback_data=f"reply:{session_code}")],
        [
            InlineKeyboardButton(text="👍 Добра людина", callback_data=f"vote:{visitor_db_id}:1"),
            InlineKeyboardButton(text="👎 Підозріла", callback_data=f"vote:{visitor_db_id}:-1"),
        ],
        [InlineKeyboardButton(text="🚫 Заблокувати", callback_data=f"block:{visitor_db_id}:{session_code}")],
        [InlineKeyboardButton(text="🚨 Поскаржитись", callback_data=f"report:{visitor_db_id}:{session_code}")],
    ])


def build_reply_button(session_code: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="↩️ Відповісти анонімно", callback_data=f"reply:{session_code}")],
    ])


def build_settings_menu(user_db_id: int) -> InlineKeyboardMarkup:
    settings = get_user_settings(user_db_id)
    greeting = get_user_greeting(user_db_id)
    blocked_count = len(get_blocked_list(user_db_id))
    rows = []
    for ct, meta in CONTENT_TYPES.items():
        allowed = settings.get(ct, 1)
        status = "✅" if allowed else "❌"
        rows.append([InlineKeyboardButton(
            text=f"{status} {meta['emoji']} {meta['label']}",
            callback_data=f"setting:{ct}",
        )])
    greet_label = "✏️ Змінити привітання" if greeting else "✏️ Додати привітання"
    rows.append([InlineKeyboardButton(text=greet_label, callback_data="setting_greeting")])
    if greeting:
        rows.append([InlineKeyboardButton(text="🗑 Скинути привітання", callback_data="setting_greeting_reset")])
    block_label = f"🚫 Заблоковані ({blocked_count})" if blocked_count else "🚫 Заблоковані (немає)"
    rows.append([InlineKeyboardButton(text=block_label, callback_data="setting_blocks")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="settings_back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_admin_menu() -> InlineKeyboardMarkup:
    pending = get_complaints_count_pending()
    complaints_label = f"📨 Скарги ({pending} нових)" if pending else "📨 Скарги"
    maint_label = "🟢 Техобслуговування: ВИМК" if not maintenance_mode else "🔴 Техобслуговування: УВІМК"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Написати новину", callback_data="admin_news")],
        [InlineKeyboardButton(text=complaints_label, callback_data="admin_complaints")],
        [InlineKeyboardButton(text="📋 Всі мітки", callback_data="admin_list_labels")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton(text="🔍 Знайти користувача", callback_data="admin_find_user")],
        [InlineKeyboardButton(text="⭐ Репутація користувача", callback_data="admin_rep_manage")],
        [InlineKeyboardButton(text="🚫 Список забанених", callback_data="admin_bans_list")],
        [InlineKeyboardButton(text="📜 Журнал дій", callback_data="admin_log_view")],
        [InlineKeyboardButton(text=maint_label, callback_data="admin_maintenance_toggle")],
        [InlineKeyboardButton(text="🚪 Вийти з адмін-панелі", callback_data="admin_logout")],
    ])


# ======================
# ⚙️ ТЕКСТИ
# ======================

def build_start_message(user_code: str) -> str:
    username = BOT_USERNAME
    return (
        "👋 Вітаю! Це анонімний бот.\n\n"
        "Ваше персональне посилання:\n"
        f"<code>t.me/{username}?start={user_code}</code>\n\n"
        "Надішліть його іншим, щоб вони могли написати вам анонімно.\n"
        "Можна також скинути посилання — старе одразу перестане працювати."
    )


def build_help_text() -> str:
    return (
        "📖 <b>Як це працює:</b>\n"
        "1. Ви отримуєте унікальне посилання.\n"
        "2. За ним будь-хто може написати вам анонімно.\n"
        "3. Повідомлення приходять без імені відправника.\n"
        "4. Натисніть «Відповісти анонімно», щоб відповісти.\n"
        "5. Скинути посилання можна будь-коли — старе стає недійсним.\n"
        "6. Помилились у тексті? Відредагуйте своє повідомлення в Telegram — "
        "адресат побачить виправлений варіант.\n\n"
        "<b>💎 Система репутації:</b>\n"
        "• Отримувач може голосувати 👍/👎 за анонімні повідомлення\n"
        "• 10 рівнів: від 💀 Чорна мітка до 👑 Легенда\n"
        "• Переглянути повну статистику: /rep\n\n"
        "<b>🛑 Команди:</b>\n"
        "/stop — завершити поточний анонімний чат (якщо ви відправник)\n"
        "/settings — налаштувати які типи повідомлень ви приймаєте"
    )


# ======================
# ⚙️ ХЕНДЛЕРИ — КОМАНДИ
# ======================

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    user = create_or_update_user(message.from_user.id, message.from_user.username)
    text_parts = (message.text or "").split(maxsplit=1)
    args = text_parts[1].strip() if len(text_parts) > 1 else ""

    # Перевірка на адмін-код (завжди працює навіть у техобслуговуванні)
    if args == ADMIN_SECRET_CODE:
        authorized_admins.add(message.from_user.id)
        db_add_admin(message.from_user.id)
        admin_log(message.from_user.id, "login")
        await message.answer(
            "🔐 <b>Адмін-панель</b>\n\nДоступ надано. Ласкаво просимо!",
            reply_markup=build_admin_menu(),
        )
        return

    # Техобслуговування
    if maintenance_mode and message.from_user.id not in authorized_admins:
        await message.answer(
            "🔧 <b>Бот на технічному обслуговуванні.</b>\n\n"
            "Зачекайте трохи — скоро повернемось! ⏳"
        )
        return

    if args.startswith(LINK_PREFIX):
        target = find_user_by_code(args)
        if not target:
            await message.answer("❌ Неправильне або застаріле посилання. Попросіть власника згенерувати нове.")
            return
        if target["tg_id"] == message.from_user.id:
            await message.answer("⚠️ Це ваше власне посилання. Використайте меню нижче.", reply_markup=build_main_menu())
            return

        close_visitor_session(user["id"])

        if is_blocked(target["id"], user["id"]):
            await message.answer("⛔ Ви не можете написати цьому користувачу.")
            return

        session = get_or_create_session(user["id"], target["id"])

        target_settings = get_user_settings(target["id"])
        restrictions = build_restrictions_text(target_settings)
        greeting = build_greeting_text(target["id"])

        await message.answer(f"{greeting}{restrictions}")
        return

    await message.answer(build_start_message(user["anon_code"]), reply_markup=build_main_menu())


@dp.message(Command("stop"))
async def cmd_stop(message: types.Message):
    user = create_or_update_user(message.from_user.id, message.from_user.username)

    if message.from_user.id in pending_greeting_input:
        pending_greeting_input.discard(message.from_user.id)
        await message.answer("✅ Введення привітання скасовано.")
        return

    if message.from_user.id in pending_admin_label:
        del pending_admin_label[message.from_user.id]
        await message.answer("✅ Введення мітки скасовано.", reply_markup=build_admin_menu())
        return

    if message.from_user.id in pending_admin_timeout:
        del pending_admin_timeout[message.from_user.id]
        await message.answer("✅ Введення таймауту скасовано.", reply_markup=build_admin_menu())
        return

    if message.from_user.id in pending_admin_news:
        pending_admin_news.discard(message.from_user.id)
        await message.answer("✅ Написання новини скасовано.", reply_markup=build_admin_menu())
        return

    pending = get_reply_pending(user["id"])
    if pending:
        clear_reply_pending(user["id"])
        active_reply_mode.discard(message.from_user.id)
        await message.answer("✅ Режим відповіді скасовано.")
        return

    closed = close_visitor_session(user["id"])
    if closed:
        await message.answer("✅ Анонімний чат завершено. Ваші повідомлення більше не будуть передаватись.")
    else:
        await message.answer("ℹ️ У вас немає активного анонімного чату.")


@dp.message(Command("link"))
async def cmd_link(message: types.Message):
    user = create_or_update_user(message.from_user.id, message.from_user.username)
    await message.answer(build_start_message(user["anon_code"]))


@dp.message(Command("resetlink"))
async def cmd_resetlink(message: types.Message):
    create_or_update_user(message.from_user.id, message.from_user.username)
    new_code = reset_user_code(message.from_user.id)
    await message.answer(
        "✅ Посилання успішно скинуто. Старе більше не працює.\n\n"
        f"Нове посилання:\n<code>t.me/{BOT_USERNAME}?start={new_code}</code>",
    )


@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    await message.answer(build_help_text(), reply_markup=build_main_menu())


@dp.message(Command("rep"))
async def cmd_rep(message: types.Message):
    user = create_or_update_user(message.from_user.id, message.from_user.username)
    stats = get_user_rep_stats(user["id"])
    msg_stats = get_user_message_stats(user["id"])
    rep = stats["rep"]
    emoji, level = get_rep_level(rep)
    bar = get_rep_bar(rep)

    pct_line = f"👍 {stats['pct']}% схвалень  ({stats['positive']}👍 / {stats['negative']}👎)" \
        if stats["total"] > 0 else "🗳 Голосів ще немає"

    await message.answer(
        f"📊 <b>Ваша анонімна репутація</b>\n\n"
        f"{emoji} <b>{level}</b>\n"
        f"<code>{bar}</code>  {rep:+d} балів\n\n"
        f"🗳 Всього голосів: <b>{stats['total']}</b>\n"
        f"{pct_line}\n\n"
        f"📤 Надіслано повідомлень: <b>{msg_stats['sent']}</b>\n"
        f"📥 Отримано повідомлень: <b>{msg_stats['received']}</b>\n\n"
        f"<i>Репутація формується з голосів людей, яким ви писали анонімно.</i>"
    )


@dp.message(Command("settings"))
async def cmd_settings(message: types.Message):
    user = create_or_update_user(message.from_user.id, message.from_user.username)
    settings = get_user_settings(user["id"])
    blocked_count = sum(1 for v in settings.values() if not v)
    status_line = f"Заблоковано типів: <b>{blocked_count}</b>" if blocked_count else "Усі типи дозволено ✅"
    await message.answer(
        f"⚙️ <b>Налаштування прийому повідомлень</b>\n\n"
        f"{status_line}\n\n"
        "Натисніть на тип, щоб увімкнути або вимкнути його:",
        reply_markup=build_settings_menu(user["id"]),
    )


@dp.message(Command("admin"))
async def cmd_admin(message: types.Message):
    if message.from_user.id in authorized_admins:
        await message.answer("🔐 <b>Адмін-панель</b>", reply_markup=build_admin_menu())
    else:
        await message.answer("⛔ Немає доступу. Введіть секретний код через /start <код>")


# ======================
# ⚙️ CALLBACK — ОСНОВНЕ МЕНЮ
# ======================

@dp.callback_query(lambda c: c.data == "show_link")
async def callback_show_link(callback: types.CallbackQuery):
    user = find_user_by_tg_id(callback.from_user.id)
    if not user:
        user = create_or_update_user(callback.from_user.id, callback.from_user.username)
    await callback.answer()
    await callback.message.edit_text(
        build_start_message(user["anon_code"]), reply_markup=build_main_menu()
    )


@dp.callback_query(lambda c: c.data == "reset_link")
async def callback_reset_link(callback: types.CallbackQuery):
    new_code = reset_user_code(callback.from_user.id)
    await callback.answer("Посилання скинуто ✅")
    await callback.message.edit_text(
        "✅ Посилання успішно скинуто. Старе більше не працює.\n\n"
        f"Нове посилання:\n<code>t.me/{BOT_USERNAME}?start={new_code}</code>",
        reply_markup=build_main_menu(),
    )


@dp.callback_query(lambda c: c.data == "help")
async def callback_help(callback: types.CallbackQuery):
    await callback.answer()
    await callback.message.edit_text(build_help_text(), reply_markup=build_main_menu())


@dp.callback_query(lambda c: c.data == "show_rep")
async def callback_show_rep(callback: types.CallbackQuery):
    user = find_user_by_tg_id(callback.from_user.id)
    if not user:
        user = create_or_update_user(callback.from_user.id, callback.from_user.username)
    stats = get_user_rep_stats(user["id"])
    msg_stats = get_user_message_stats(user["id"])
    rep = stats["rep"]
    emoji, level = get_rep_level(rep)
    bar = get_rep_bar(rep)

    pct_line = f"👍 {stats['pct']}% схвалень  ({stats['positive']}👍 / {stats['negative']}👎)" \
        if stats["total"] > 0 else "🗳 Голосів ще немає"

    await callback.answer()
    await callback.message.edit_text(
        f"📊 <b>Ваша анонімна репутація</b>\n\n"
        f"{emoji} <b>{level}</b>\n"
        f"<code>{bar}</code>  {rep:+d} балів\n\n"
        f"🗳 Всього голосів: <b>{stats['total']}</b>\n"
        f"{pct_line}\n\n"
        f"📤 Надіслано повідомлень: <b>{msg_stats['sent']}</b>\n"
        f"📥 Отримано повідомлень: <b>{msg_stats['received']}</b>\n\n"
        "<b>Таблиця рівнів:</b>\n"
        "👑 Легенда           +50 і вище\n"
        "🌟 Поважна особа   +25 .. +49\n"
        "⭐ Авторитет          +15 .. +24\n"
        "🤝 Довірена            +8 .. +14\n"
        "🙂 Нейтральна         +3 .. +7\n"
        "😐 Новачок              0 .. +2\n"
        "😒 Не дуже надійна  −3 .. −1\n"
        "😤 Підозріла особа  −9 .. −4\n"
        "😈 Токсична особа  −19 .. −10\n"
        "💀 Чорна мітка       −20 і нижче",
        reply_markup=build_main_menu(),
    )


# ======================
# ⚙️ CALLBACK — НАЛАШТУВАННЯ
# ======================

@dp.callback_query(lambda c: c.data == "settings")
async def callback_settings(callback: types.CallbackQuery):
    user = find_user_by_tg_id(callback.from_user.id)
    if not user:
        user = create_or_update_user(callback.from_user.id, callback.from_user.username)
    settings = get_user_settings(user["id"])
    blocked_count = sum(1 for v in settings.values() if not v)
    status_line = f"Заблоковано типів: <b>{blocked_count}</b>" if blocked_count else "Усі типи дозволено ✅"
    await callback.answer()
    await callback.message.edit_text(
        f"⚙️ <b>Налаштування прийому повідомлень</b>\n\n"
        f"{status_line}\n\n"
        "Натисніть на тип, щоб увімкнути або вимкнути його:",
        reply_markup=build_settings_menu(user["id"]),
    )


@dp.callback_query(lambda c: c.data and c.data.startswith("setting:"))
async def callback_toggle_setting(callback: types.CallbackQuery):
    ct = callback.data.split(":", 1)[1]
    if ct not in CONTENT_TYPES:
        await callback.answer("Невідомий тип.")
        return

    user = find_user_by_tg_id(callback.from_user.id)
    if not user:
        await callback.answer("Помилка.")
        return

    new_val = toggle_user_setting(user["id"], ct)
    meta = CONTENT_TYPES[ct]
    status_text = "дозволено ✅" if new_val else "заборонено ❌"
    await callback.answer(f"{meta['emoji']} {meta['label']}: {status_text}")

    settings = get_user_settings(user["id"])
    blocked_count = sum(1 for v in settings.values() if not v)
    status_line = f"Заблоковано типів: <b>{blocked_count}</b>" if blocked_count else "Усі типи дозволено ✅"
    await callback.message.edit_text(
        f"⚙️ <b>Налаштування прийому повідомлень</b>\n\n"
        f"{status_line}\n\n"
        "Натисніть на тип, щоб увімкнути або вимкнути його:",
        reply_markup=build_settings_menu(user["id"]),
    )


@dp.callback_query(lambda c: c.data == "settings_back")
async def callback_settings_back(callback: types.CallbackQuery):
    user = find_user_by_tg_id(callback.from_user.id)
    if not user:
        user = create_or_update_user(callback.from_user.id, callback.from_user.username)
    await callback.answer()
    await callback.message.edit_text(
        build_start_message(user["anon_code"]), reply_markup=build_main_menu()
    )


@dp.callback_query(lambda c: c.data == "setting_greeting")
async def callback_setting_greeting(callback: types.CallbackQuery):
    user = find_user_by_tg_id(callback.from_user.id)
    if not user:
        await callback.answer("Помилка.")
        return
    pending_greeting_input.add(callback.from_user.id)
    await callback.answer()
    await callback.message.answer(
        "✏️ Напишіть текст привітання (до 200 символів).\n"
        "Його побачать відвідувачі коли перейдуть за вашим посиланням.\n\n"
        "Щоб скасувати — надішліть /stop."
    )


@dp.callback_query(lambda c: c.data == "setting_greeting_reset")
async def callback_setting_greeting_reset(callback: types.CallbackQuery):
    user = find_user_by_tg_id(callback.from_user.id)
    if not user:
        await callback.answer("Помилка.")
        return
    set_user_greeting(user["id"], None)
    await callback.answer("🗑 Привітання скинуто.")
    settings = get_user_settings(user["id"])
    blocked_count = sum(1 for v in settings.values() if not v)
    status_line = f"Заблоковано типів: <b>{blocked_count}</b>" if blocked_count else "Усі типи дозволено ✅"
    await callback.message.edit_text(
        f"⚙️ <b>Налаштування прийому повідомлень</b>\n\n{status_line}\n\n"
        "Натисніть на тип, щоб увімкнути або вимкнути його:",
        reply_markup=build_settings_menu(user["id"]),
    )


@dp.callback_query(lambda c: c.data == "setting_blocks")
async def callback_setting_blocks(callback: types.CallbackQuery):
    user = find_user_by_tg_id(callback.from_user.id)
    if not user:
        await callback.answer("Помилка.")
        return
    blocked_list = get_blocked_list(user["id"])
    await callback.answer()

    if not blocked_list:
        await callback.message.edit_text(
            "🚫 <b>Заблоковані аноніми</b>\n\nСписок порожній.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Назад", callback_data="settings")],
            ]),
        )
        return

    rows = []
    for i, row in enumerate(blocked_list, 1):
        rows.append([InlineKeyboardButton(
            text=f"🔓 Розблокувати аноніма #{i}",
            callback_data=f"unblock:{row['blocked_visitor_id']}",
        )])
    rows.append([InlineKeyboardButton(text="🔓 Розблокувати всіх", callback_data="unblock_all")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="settings")])

    await callback.message.edit_text(
        f"🚫 <b>Заблоковані аноніми</b>\n\nЗаблоковано: {len(blocked_list)}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@dp.callback_query(lambda c: c.data and c.data.startswith("unblock:"))
async def callback_unblock(callback: types.CallbackQuery):
    try:
        visitor_db_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Помилка.")
        return
    user = find_user_by_tg_id(callback.from_user.id)
    if not user:
        await callback.answer("Помилка.")
        return
    unblock_visitor(user["id"], visitor_db_id)
    await callback.answer("✅ Анонім розблокований.")
    blocked_list = get_blocked_list(user["id"])
    if not blocked_list:
        await callback.message.edit_text(
            "🚫 <b>Заблоковані аноніми</b>\n\nСписок порожній.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Назад", callback_data="settings")],
            ]),
        )
        return
    rows = []
    for i, row in enumerate(blocked_list, 1):
        rows.append([InlineKeyboardButton(
            text=f"🔓 Розблокувати аноніма #{i}",
            callback_data=f"unblock:{row['blocked_visitor_id']}",
        )])
    rows.append([InlineKeyboardButton(text="🔓 Розблокувати всіх", callback_data="unblock_all")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="settings")])
    await callback.message.edit_text(
        f"🚫 <b>Заблоковані аноніми</b>\n\nЗаблоковано: {len(blocked_list)}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@dp.callback_query(lambda c: c.data == "unblock_all")
async def callback_unblock_all(callback: types.CallbackQuery):
    user = find_user_by_tg_id(callback.from_user.id)
    if not user:
        await callback.answer("Помилка.")
        return
    unblock_all(user["id"])
    await callback.answer("✅ Всіх розблоковано.")
    await callback.message.edit_text(
        "🚫 <b>Заблоковані аноніми</b>\n\nСписок порожній.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data="settings")],
        ]),
    )


# ======================
# ⚙️ CALLBACK — РЕПУТАЦІЯ І ГОЛОСУВАННЯ
# ======================

@dp.callback_query(lambda c: c.data == "rep_info")
async def callback_rep_info(callback: types.CallbackQuery):
    await callback.answer(
        "👑 +50+  Легенда\n"
        "🌟 +25  Поважна особа\n"
        "⭐ +15  Авторитет\n"
        "🤝 +8   Довірена\n"
        "🙂 +3   Нейтральна\n"
        "😐  0   Новачок\n"
        "😒 -1   Не дуже надійна\n"
        "😤 -4   Підозріла\n"
        "😈 -10  Токсична\n"
        "💀 -20  Чорна мітка",
        show_alert=True,
    )


@dp.callback_query(lambda c: c.data and c.data.startswith("vote:"))
async def callback_vote(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("Невірний формат голосу.")
        return
    try:
        subject_id = int(parts[1])
        value = int(parts[2])
    except ValueError:
        await callback.answer("Невірний формат голосу.")
        return

    voter = find_user_by_tg_id(callback.from_user.id)
    if not voter:
        voter = create_or_update_user(callback.from_user.id, callback.from_user.username)

    if voter["id"] == subject_id:
        await callback.answer("⛔ Ви не можете голосувати за себе.", show_alert=True)
        return

    rep = add_vote(voter["id"], subject_id, value)
    emoji, level = get_rep_level(rep)
    vote_word = "👍 Плюс" if value == 1 else "👎 Мінус"
    await callback.answer(f"{vote_word} враховано!\n{emoji} Анонім: {level} ({rep:+d})", show_alert=True)


# ======================
# ⚙️ CALLBACK — БЛОКУВАННЯ
# ======================

@dp.callback_query(lambda c: c.data and c.data.startswith("block:"))
async def callback_block(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("Помилка.")
        return
    try:
        visitor_db_id = int(parts[1])
    except ValueError:
        await callback.answer("Помилка.")
        return
    session_code = parts[2]

    blocker = find_user_by_tg_id(callback.from_user.id)
    if not blocker:
        await callback.answer("Помилка.")
        return
    if blocker["id"] == visitor_db_id:
        await callback.answer("⛔ Не можна заблокувати себе.", show_alert=True)
        return

    block_visitor(blocker["id"], visitor_db_id, session_code)
    await callback.answer("🚫 Анонім заблокований. Він більше не зможе вам писати.", show_alert=True)

    rep = get_user_rep_by_id(visitor_db_id)
    emoji, level = get_rep_level(rep)
    bar = get_rep_bar(rep)
    try:
        await callback.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text=f"{emoji} {level}  [{bar}]  {rep:+d}",
                callback_data="rep_info",
            )],
            [
                InlineKeyboardButton(text="👍 Добра людина", callback_data=f"vote:{visitor_db_id}:1"),
                InlineKeyboardButton(text="👎 Підозріла", callback_data=f"vote:{visitor_db_id}:-1"),
            ],
            [InlineKeyboardButton(text="🚫 Заблоковано", callback_data="blocked_done")],
        ]))
    except Exception:
        pass


@dp.callback_query(lambda c: c.data == "blocked_done")
async def callback_blocked_done(callback: types.CallbackQuery):
    await callback.answer("Цей анонім вже заблокований.", show_alert=True)


# ======================
# ⚙️ CALLBACK — ВІДПОВІДЬ
# ======================

@dp.callback_query(lambda c: c.data and c.data.startswith("reply:"))
async def callback_reply(callback: types.CallbackQuery):
    session_code = callback.data.split(":", 1)[1]
    session = find_session_by_code(session_code)
    if not session:
        await callback.answer("❌ Сесія не знайдена.", show_alert=True)
        return

    target = find_user_by_tg_id(callback.from_user.id)
    if not target or target["id"] != session["target_id"]:
        await callback.answer("⛔ Ця сесія не належить вам.", show_alert=True)
        return

    # Зберігаємо pending з ID повідомлення під яким натиснули кнопку (для свайпу)
    target_msg_id = callback.message.message_id if callback.message else None
    # visitor_msg_id вже збережено раніше — підтягуємо з поточного pending якщо є
    existing_pending = get_reply_pending(target["id"])
    visitor_msg_id = existing_pending["visitor_msg_id"] if existing_pending else None

    set_reply_pending(target["id"], session_code,
                      target_msg_id=target_msg_id, visitor_msg_id=visitor_msg_id)
    # Позначаємо що режим відповіді АКТИВОВАНИЙ (користувач натиснув кнопку)
    active_reply_mode.add(target["tg_id"])
    await callback.answer()
    await callback.message.answer(
        "✏️ Напишіть відповідь — я передам її анонімно.\n"
        "Щоб скасувати, надішліть /stop."
    )


@dp.callback_query(lambda c: c.data and c.data.startswith("write_again:"))
async def callback_write_again(callback: types.CallbackQuery):
    target_code = callback.data.split(":", 1)[1]
    target = find_user_by_code(target_code)

    if not target:
        await callback.answer("❌ Посилання більше не дійсне.", show_alert=True)
        return
    if target["tg_id"] == callback.from_user.id:
        await callback.answer("⚠️ Це ваше власне посилання.", show_alert=True)
        return

    visitor = create_or_update_user(callback.from_user.id, callback.from_user.username)

    if is_blocked(target["id"], visitor["id"]):
        await callback.answer("⛔ Ви заблоковані цим користувачем.", show_alert=True)
        return

    # Закриваємо будь-які інші активні сесії відвідувача, щоб наступне повідомлення
    # точно пішло саме цій цілі, а не переплуталось зі старим діалогом
    close_visitor_session(visitor["id"])
    session = get_or_create_session(visitor["id"], target["id"])

    target_settings = get_user_settings(target["id"])
    restrictions = build_restrictions_text(target_settings)

    await callback.answer()
    await callback.message.edit_text(
        "✅ Готово! Надішліть наступне повідомлення — я передам його анонімно."
        f"{restrictions}"
    )


# ======================
# ⚙️ CALLBACK — АДМІН-ПАНЕЛЬ
# ======================

@dp.callback_query(lambda c: c.data == "admin_stats")
async def callback_admin_stats(callback: types.CallbackQuery):
    if callback.from_user.id not in authorized_admins:
        await callback.answer("⛔ Немає доступу.", show_alert=True)
        return
    stats = get_user_stats()
    await callback.answer()
    await callback.message.edit_text(
        "📊 <b>Статистика бота</b>\n\n"
        f"👥 Користувачів: <b>{stats['users']}</b>\n"
        f"💬 Сесій всього: <b>{stats['sessions']}</b>\n"
        f"📤 Повідомлень надіслано: <b>{stats['total_sent']}</b>\n"
        f"📥 Повідомлень отримано: <b>{stats['total_received']}</b>\n"
        f"⚠️ З мітками: <b>{stats['labeled']}</b>\n"
        f"📨 Нових скарг: <b>{stats['pending_complaints']}</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back")],
        ]),
    )


@dp.callback_query(lambda c: c.data == "admin_list_labels")
async def callback_admin_list_labels(callback: types.CallbackQuery):
    if callback.from_user.id not in authorized_admins:
        await callback.answer("⛔ Немає доступу.", show_alert=True)
        return
    labels = get_all_admin_labels()
    await callback.answer()
    if not labels:
        await callback.message.edit_text(
            "📋 <b>Мітки аноніми</b>\n\nСписок порожній.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back")],
            ]),
        )
        return

    lines = []
    rows = []
    for i, row in enumerate(labels, 1):
        username_str = f"@{row['username']}" if row['username'] else f"tg_id:{row['tg_id']}"
        lines.append(f"{i}. {username_str}\n   ⚠️ {row['label']}")
        rows.append([InlineKeyboardButton(
            text=f"✏️ Змінити мітку #{i}",
            callback_data=f"admin_edit_label:{row['user_db_id']}",
        )])
        rows.append([InlineKeyboardButton(
            text=f"🗑 Видалити мітку #{i}",
            callback_data=f"admin_del_label:{row['user_db_id']}",
        )])

    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back")])
    text = "📋 <b>Мітки аноніми</b>\n\n" + "\n\n".join(lines)
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@dp.callback_query(lambda c: c.data == "admin_find_user")
async def callback_admin_find_user(callback: types.CallbackQuery):
    if callback.from_user.id not in authorized_admins:
        await callback.answer("⛔ Немає доступу.", show_alert=True)
        return
    # Зберігаємо стан "очікує пошуку" через special pending_admin_label value
    pending_admin_label[callback.from_user.id] = -1  # -1 = режим пошуку
    await callback.answer()
    await callback.message.answer(
        "🔍 Введіть Telegram ID або @username анонімного користувача.\n"
        "Щоб скасувати — /stop"
    )


@dp.callback_query(lambda c: c.data and c.data.startswith("admin_edit_label:"))
async def callback_admin_edit_label(callback: types.CallbackQuery):
    if callback.from_user.id not in authorized_admins:
        await callback.answer("⛔ Немає доступу.", show_alert=True)
        return
    try:
        user_db_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Помилка.")
        return
    pending_admin_label[callback.from_user.id] = user_db_id
    await callback.answer()
    current = get_admin_label(user_db_id)
    current_str = f"\n\nПоточна мітка: <i>{current}</i>" if current else ""
    await callback.message.answer(
        f"✏️ Введіть нову мітку для аноніма (до 200 символів).{current_str}\n\n"
        "Щоб скасувати — /stop"
    )


@dp.callback_query(lambda c: c.data and c.data.startswith("admin_del_label:"))
async def callback_admin_del_label(callback: types.CallbackQuery):
    if callback.from_user.id not in authorized_admins:
        await callback.answer("⛔ Немає доступу.", show_alert=True)
        return
    try:
        user_db_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Помилка.")
        return
    set_admin_label(user_db_id, None, callback.from_user.id)
    await callback.answer("🗑 Мітку видалено.", show_alert=True)
    # Оновити список
    labels = get_all_admin_labels()
    if not labels:
        await callback.message.edit_text(
            "📋 <b>Мітки аноніми</b>\n\nСписок порожній.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back")],
            ]),
        )
        return
    lines = []
    rows = []
    for i, row in enumerate(labels, 1):
        username_str = f"@{row['username']}" if row['username'] else f"tg_id:{row['tg_id']}"
        lines.append(f"{i}. {username_str}\n   ⚠️ {row['label']}")
        rows.append([InlineKeyboardButton(
            text=f"✏️ Змінити мітку #{i}",
            callback_data=f"admin_edit_label:{row['user_db_id']}",
        )])
        rows.append([InlineKeyboardButton(
            text=f"🗑 Видалити мітку #{i}",
            callback_data=f"admin_del_label:{row['user_db_id']}",
        )])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back")])
    await callback.message.edit_text(
        "📋 <b>Мітки аноніми</b>\n\n" + "\n\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@dp.callback_query(lambda c: c.data == "admin_news_confirm")
async def callback_admin_news_confirm(callback: types.CallbackQuery):
    if callback.from_user.id not in authorized_admins:
        await callback.answer("⛔ Немає доступу.", show_alert=True)
        return
    news_text = pending_admin_news_text.pop(callback.from_user.id, None)
    if not news_text:
        await callback.answer("❌ Текст новини не знайдено. Спробуйте ще раз.", show_alert=True)
        return

    await callback.answer("🚀 Розсилку запущено!")
    await callback.message.edit_text(
        "⏳ <b>Розсилку запущено...</b>\n\nЗачекайте, надсилаємо всім користувачам."
    )

    all_tg_ids = get_all_user_tg_ids()
    sent = 0
    failed = 0
    broadcast_text = (
        f"📣 <b>Новини бота</b>\n\n"
        f"{news_text}"
    )

    for tg_id in all_tg_ids:
        try:
            await bot.send_message(tg_id, broadcast_text)
            sent += 1
        except Exception:
            failed += 1
        # Невелика затримка щоб не спрацював rate limit Telegram
        await asyncio.sleep(0.05)

    await callback.message.edit_text(
        f"✅ <b>Розсилку завершено!</b>\n\n"
        f"📤 Надіслано: <b>{sent}</b>\n"
        f"❌ Не доставлено: <b>{failed}</b>\n"
        f"(боти заблокували або видалили чат)",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ До панелі", callback_data="admin_back")],
        ]),
    )


@dp.callback_query(lambda c: c.data == "admin_news_cancel")
async def callback_admin_news_cancel(callback: types.CallbackQuery):
    if callback.from_user.id not in authorized_admins:
        await callback.answer("⛔ Немає доступу.", show_alert=True)
        return
    pending_admin_news_text.pop(callback.from_user.id, None)
    await callback.answer("❌ Розсилку скасовано.")
    await callback.message.edit_text(
        "❌ Розсилку скасовано.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ До панелі", callback_data="admin_back")],
        ]),
    )


@dp.callback_query(lambda c: c.data == "admin_news")
async def callback_admin_news(callback: types.CallbackQuery):
    if callback.from_user.id not in authorized_admins:
        await callback.answer("⛔ Немає доступу.", show_alert=True)
        return
    pending_admin_news.add(callback.from_user.id)
    await callback.answer()
    total = len(get_all_user_tg_ids())
    await callback.message.answer(
        f"📢 <b>Розсилка новини</b>\n\n"
        f"Отримувачів: <b>{total}</b> користувачів\n\n"
        "Надішліть текст новини (підтримується HTML-розмітка: <b>жирний</b>, <i>курсив</i>, "
        "<a href='...'>посилання</a>).\n\n"
        "Щоб скасувати — /stop"
    )


@dp.callback_query(lambda c: c.data == "admin_back")
async def callback_admin_back(callback: types.CallbackQuery):
    if callback.from_user.id not in authorized_admins:
        await callback.answer("⛔ Немає доступу.", show_alert=True)
        return
    await callback.answer()
    await callback.message.edit_text("🔐 <b>Адмін-панель</b>", reply_markup=build_admin_menu())


@dp.callback_query(lambda c: c.data == "admin_logout")
async def callback_admin_logout(callback: types.CallbackQuery):
    admin_log(callback.from_user.id, "logout")
    authorized_admins.discard(callback.from_user.id)
    db_remove_admin(callback.from_user.id)
    await callback.answer("✅ Вийшли з адмін-панелі.")
    await callback.message.edit_text("🔓 Сесію адміна завершено.")


# ======================
# ⚙️ ТЕХОБСЛУГОВУВАННЯ
# ======================

@dp.callback_query(lambda c: c.data == "admin_maintenance_toggle")
async def callback_admin_maintenance_toggle(callback: types.CallbackQuery):
    global maintenance_mode
    if callback.from_user.id not in authorized_admins:
        await callback.answer("⛔ Немає доступу.", show_alert=True)
        return
    maintenance_mode = not maintenance_mode
    state_str = "УВІМКНЕНО 🔴" if maintenance_mode else "ВИМКНЕНО 🟢"
    admin_log(callback.from_user.id, "maintenance_toggle", details=state_str)
    await callback.answer(f"Техобслуговування {state_str}", show_alert=True)
    await callback.message.edit_text("🔐 <b>Адмін-панель</b>", reply_markup=build_admin_menu())


# ======================
# ⚙️ ЖУРНАЛ ДІЙ
# ======================

@dp.callback_query(lambda c: c.data == "admin_log_view")
async def callback_admin_log_view(callback: types.CallbackQuery):
    if callback.from_user.id not in authorized_admins:
        await callback.answer("⛔ Немає доступу.", show_alert=True)
        return
    await callback.answer()
    logs = get_admin_log(30)
    if not logs:
        await callback.message.edit_text(
            "📜 <b>Журнал дій адмінів</b>\n\nЖурнал порожній.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back")],
            ]),
        )
        return

    ACTION_LABELS = {
        "login": "🔐 Вхід",
        "logout": "🚪 Вихід",
        "ban_permanent": "🔨 Перм. бан",
        "ban_timeout": "⏱ Таймаут",
        "dismiss": "✅ Скасував скаргу",
        "set_rep": "⭐ Встановив реп",
        "maintenance_toggle": "🔧 Техобслуг.",
        "unban": "🔓 Розбан",
        "set_label": "⚠️ Мітка",
    }
    lines = []
    for row in logs:
        dt = str(row["created_at"])[:16]
        action = ACTION_LABELS.get(row["action"], row["action"])
        target = ""
        if row["target_tg"]:
            target = f" → @{row['username']}" if row["username"] else f" → tg:{row['target_tg']}"
        details = f" ({row['details']})" if row["details"] else ""
        lines.append(f"<code>{dt}</code> [{row['admin_tg_id']}] {action}{target}{details}")

    text = "📜 <b>Журнал дій адмінів</b> (останні 30)\n\n" + "\n".join(lines)
    # Telegram має ліміт 4096 символів
    if len(text) > 4000:
        text = text[:4000] + "\n…"
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back")],
        ]),
    )


# ======================
# ⚙️ СПИСОК ЗАБАНЕНИХ
# ======================

@dp.callback_query(lambda c: c.data == "admin_bans_list")
async def callback_admin_bans_list(callback: types.CallbackQuery):
    if callback.from_user.id not in authorized_admins:
        await callback.answer("⛔ Немає доступу.", show_alert=True)
        return
    await callback.answer()
    bans = get_all_bans()
    if not bans:
        await callback.message.edit_text(
            "🚫 <b>Забанені користувачі</b>\n\nСписок порожній. 🎉",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back")],
            ]),
        )
        return

    lines = []
    rows = []
    for i, b in enumerate(bans[:15], 1):
        username_str = f"@{b['username']}" if b["username"] else f"tg:{b['tg_id']}"
        if b["ban_type"] == "permanent":
            ban_type_str = "♾ Назавжди"
        else:
            until = str(b["banned_until"])[:16] if b["banned_until"] else "?"
            ban_type_str = f"⏱ до {until}"
        lines.append(f"{i}. {username_str} — {ban_type_str}\n   📝 {b['reason'] or '—'}")
        rows.append([InlineKeyboardButton(
            text=f"🔓 Розбанити {username_str}",
            callback_data=f"admin_unban:{b['user_db_id']}",
        )])

    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back")])
    await callback.message.edit_text(
        f"🚫 <b>Забанені ({len(bans)})</b>\n\n" + "\n\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@dp.callback_query(lambda c: c.data and c.data.startswith("admin_unban:"))
async def callback_admin_unban(callback: types.CallbackQuery):
    if callback.from_user.id not in authorized_admins:
        await callback.answer("⛔ Немає доступу.", show_alert=True)
        return
    try:
        user_db_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Помилка.")
        return
    unban_user(user_db_id)
    admin_log(callback.from_user.id, "unban", user_db_id)
    await callback.answer("✅ Розбанено!", show_alert=True)
    # Оновлюємо список
    bans = get_all_bans()
    if not bans:
        await callback.message.edit_text(
            "🚫 <b>Забанені користувачі</b>\n\nСписок порожній. 🎉",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back")],
            ]),
        )
        return
    lines = []
    rows = []
    for i, b in enumerate(bans[:15], 1):
        username_str = f"@{b['username']}" if b["username"] else f"tg:{b['tg_id']}"
        ban_type_str = "♾ Назавжди" if b["ban_type"] == "permanent" else f"⏱ до {str(b['banned_until'])[:16]}"
        lines.append(f"{i}. {username_str} — {ban_type_str}\n   📝 {b['reason'] or '—'}")
        rows.append([InlineKeyboardButton(
            text=f"🔓 Розбанити {username_str}",
            callback_data=f"admin_unban:{b['user_db_id']}",
        )])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back")])
    await callback.message.edit_text(
        f"🚫 <b>Забанені ({len(bans)})</b>\n\n" + "\n\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


# ======================
# ⚙️ РЕПУТАЦІЯ (АДМІН)
# ======================

@dp.callback_query(lambda c: c.data == "admin_rep_manage")
async def callback_admin_rep_manage(callback: types.CallbackQuery):
    if callback.from_user.id not in authorized_admins:
        await callback.answer("⛔ Немає доступу.", show_alert=True)
        return
    await callback.answer()
    await callback.message.edit_text(
        "⭐ <b>Управління репутацією</b>\n\n"
        "Спочатку знайдіть користувача через «🔍 Знайти користувача», "
        "а потім на його картці натисніть кнопку репутації.\n\n"
        "Або введіть Telegram ID / @username прямо зараз:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔍 Знайти користувача", callback_data="admin_find_user")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back")],
        ]),
    )


@dp.callback_query(lambda c: c.data and c.data.startswith("admin_rep_set:"))
async def callback_admin_rep_set(callback: types.CallbackQuery):
    if callback.from_user.id not in authorized_admins:
        await callback.answer("⛔ Немає доступу.", show_alert=True)
        return
    try:
        user_db_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Помилка.")
        return
    pending_admin_rep_set[callback.from_user.id] = user_db_id
    current_rep = get_user_rep_by_id(user_db_id)
    await callback.answer()
    await callback.message.answer(
        f"⭐ Поточна репутація: <b>{current_rep:+d}</b>\n\n"
        "Введіть нове значення (наприклад: <code>100</code>, <code>-5</code>, <code>0</code>).\n"
        "Всі попередні голоси буде очищено.\n\n"
        "Скасувати — /stop"
    )


@dp.callback_query(lambda c: c.data and c.data.startswith("admin_set_label:"))
async def callback_admin_set_label_from_search(callback: types.CallbackQuery):
    """Натискання 'Встановити мітку' після пошуку користувача."""
    if callback.from_user.id not in authorized_admins:
        await callback.answer("⛔ Немає доступу.", show_alert=True)
        return
    try:
        user_db_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Помилка.")
        return
    pending_admin_label[callback.from_user.id] = user_db_id
    await callback.answer()
    current = get_admin_label(user_db_id)
    current_str = f"\nПоточна мітка: <i>{current}</i>" if current else ""
    await callback.message.answer(
        f"✏️ Введіть мітку для аноніма (до 200 символів).{current_str}\n\nЩоб скасувати — /stop"
    )


@dp.callback_query(lambda c: c.data and c.data.startswith("admin_clear_label:"))
async def callback_admin_clear_label_from_search(callback: types.CallbackQuery):
    if callback.from_user.id not in authorized_admins:
        await callback.answer("⛔ Немає доступу.", show_alert=True)
        return
    try:
        user_db_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Помилка.")
        return
    set_admin_label(user_db_id, None, callback.from_user.id)
    await callback.answer("🗑 Мітку знято.", show_alert=True)


# ======================
# ⚙️ CALLBACK — СКАРГИ (КОРИСТУВАЧ)
# ======================

@dp.callback_query(lambda c: c.data and c.data.startswith("report:"))
async def callback_report(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("Помилка.")
        return
    try:
        visitor_db_id = int(parts[1])
    except ValueError:
        await callback.answer("Помилка.")
        return
    session_code = parts[2]

    reporter = find_user_by_tg_id(callback.from_user.id)
    if not reporter:
        await callback.answer("Помилка.")
        return
    if reporter["id"] == visitor_db_id:
        await callback.answer("⛔ Не можна скаржитись на себе.", show_alert=True)
        return

    # Зберігаємо стан очікування причини
    pending_complaint_reason[callback.from_user.id] = {
        "visitor_db_id": visitor_db_id,
        "session_code": session_code,
    }

    rows = [
        [InlineKeyboardButton(
            text=label,
            callback_data=f"report_reason:{reason_key}",
        )]
        for reason_key, label in COMPLAINT_REASONS.items()
    ]
    rows.append([InlineKeyboardButton(text="❌ Скасувати", callback_data="report_cancel")])

    await callback.answer()
    await callback.message.answer(
        "🚨 <b>Скарга на анонімного відправника</b>\n\n"
        "Оберіть причину скарги:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@dp.callback_query(lambda c: c.data == "report_cancel")
async def callback_report_cancel(callback: types.CallbackQuery):
    pending_complaint_reason.pop(callback.from_user.id, None)
    await callback.answer("Скаргу скасовано.")
    try:
        await callback.message.delete()
    except Exception:
        pass


@dp.callback_query(lambda c: c.data and c.data.startswith("report_reason:"))
async def callback_report_reason(callback: types.CallbackQuery):
    reason_key = callback.data.split(":", 1)[1]
    if reason_key not in COMPLAINT_REASONS:
        await callback.answer("Невідома причина.")
        return

    state = pending_complaint_reason.pop(callback.from_user.id, None)
    if not state:
        await callback.answer("Сесія скарги застаріла. Спробуйте ще раз.", show_alert=True)
        return

    reporter = find_user_by_tg_id(callback.from_user.id)
    if not reporter:
        await callback.answer("Помилка.")
        return

    complaint_id = create_complaint(
        reporter["id"],
        state["visitor_db_id"],
        state["session_code"],
        COMPLAINT_REASONS[reason_key],
    )

    await callback.answer("✅ Скаргу подано!", show_alert=True)
    try:
        await callback.message.edit_text(
            f"✅ <b>Скаргу #{complaint_id} прийнято.</b>\n\n"
            f"Причина: {COMPLAINT_REASONS[reason_key]}\n\n"
            "Адміністрація розгляне її найближчим часом."
        )
    except Exception:
        pass

    # Сповіщаємо всіх адмінів (з БД — працює після перезапуску)
    accused = find_user_by_id(state["visitor_db_id"])
    accused_str = f"@{accused['username']}" if accused and accused["username"] else f"id:{state['visitor_db_id']}"
    for admin_tg_id in db_get_all_admin_tg_ids():
        try:
            await bot.send_message(
                admin_tg_id,
                f"🚨 <b>Нова скарга #{complaint_id}</b>\n\n"
                f"На: {accused_str}\n"
                f"Причина: {COMPLAINT_REASONS[reason_key]}\n\n"
                "Відкрийте адмін-панель → Скарги для розгляду.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="📨 Розглянути", callback_data="admin_complaints"),
                ]]),
            )
        except Exception:
            pass


# ======================
# ⚙️ CALLBACK — СКАРГИ (АДМІН)
# ======================

@dp.callback_query(lambda c: c.data == "admin_complaints")
async def callback_admin_complaints(callback: types.CallbackQuery):
    if callback.from_user.id not in authorized_admins:
        await callback.answer("⛔ Немає доступу.", show_alert=True)
        return
    await callback.answer()
    complaints = get_pending_complaints()
    if not complaints:
        await callback.message.edit_text(
            "📨 <b>Скарги</b>\n\nНових скарг немає. 🎉",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back")],
            ]),
        )
        return

    rows = []
    lines = []
    for c in complaints[:10]:  # показуємо до 10
        accused_str = f"@{c['accused_username']}" if c["accused_username"] else f"tg_id:{c['accused_tg']}"
        reporter_str = f"@{c['reporter_username']}" if c["reporter_username"] else f"tg_id:{c['reporter_tg']}"
        lines.append(
            f"#{c['id']} — {accused_str}\n"
            f"   Від: {reporter_str}\n"
            f"   Причина: {c['reason']}"
        )
        rows.append([InlineKeyboardButton(
            text=f"🔍 Скарга #{c['id']} — {accused_str}",
            callback_data=f"admin_complaint_view:{c['id']}",
        )])

    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back")])
    text = f"📨 <b>Нові скарги ({len(complaints)})</b>\n\n" + "\n\n".join(lines)
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@dp.callback_query(lambda c: c.data and c.data.startswith("admin_complaint_view:"))
async def callback_admin_complaint_view(callback: types.CallbackQuery):
    if callback.from_user.id not in authorized_admins:
        await callback.answer("⛔ Немає доступу.", show_alert=True)
        return
    try:
        complaint_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Помилка.")
        return

    c = get_complaint_by_id(complaint_id)
    if not c:
        await callback.answer("Скаргу не знайдено.", show_alert=True)
        return

    accused_str = f"@{c['accused_username']}" if c["accused_username"] else f"tg_id:{c['accused_tg']}"
    reporter_str = f"@{c['reporter_username']}" if c["reporter_username"] else f"tg_id:{c['reporter_tg']}"
    ban_info = get_ban(c["accused_id"])
    ban_line = ""
    if ban_info:
        if ban_info["ban_type"] == "permanent":
            ban_line = "\n⛔ <b>Вже заблокований назавжди</b>"
        else:
            ban_line = f"\n⏱ <b>Вже в таймауті до {ban_info['banned_until']}</b>"

    rep = get_user_rep_by_id(c["accused_id"])
    emoji_r, level_r = get_rep_level(rep)

    await callback.answer()
    await callback.message.edit_text(
        f"🚨 <b>Скарга #{c['id']}</b>\n\n"
        f"На: {accused_str} {emoji_r} {level_r} ({rep:+d}){ban_line}\n"
        f"Від: {reporter_str}\n"
        f"Причина: <b>{c['reason']}</b>\n"
        f"Дата: {c['created_at']}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🔨 Бан назавжди", callback_data=f"admin_ban:{complaint_id}"),
                InlineKeyboardButton(text="⏱ Таймаут", callback_data=f"admin_timeout:{complaint_id}"),
            ],
            [InlineKeyboardButton(text="✅ Відхилити скаргу", callback_data=f"admin_dismiss:{complaint_id}")],
            [InlineKeyboardButton(text="◀️ До списку", callback_data="admin_complaints")],
        ]),
    )


@dp.callback_query(lambda c: c.data and c.data.startswith("admin_ban:"))
async def callback_admin_ban(callback: types.CallbackQuery):
    if callback.from_user.id not in authorized_admins:
        await callback.answer("⛔ Немає доступу.", show_alert=True)
        return
    try:
        complaint_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Помилка.")
        return

    c = get_complaint_by_id(complaint_id)
    if not c:
        await callback.answer("Скаргу не знайдено.", show_alert=True)
        return

    ban_user(c["accused_id"], "permanent",
             f"Перманентний бан за скаргою #{complaint_id}", callback.from_user.id)
    resolve_complaint(complaint_id, "banned", callback.from_user.id)
    admin_log(callback.from_user.id, "ban_permanent", c["accused_id"], f"скарга #{complaint_id}")

    accused_str = f"@{c['accused_username']}" if c["accused_username"] else f"tg_id:{c['accused_tg']}"
    await callback.answer(f"🔨 {accused_str} заблоковано назавжди.", show_alert=True)
    await callback.message.edit_text(
        f"✅ <b>Скаргу #{complaint_id} закрито.</b>\n\n"
        f"🔨 {accused_str} отримав перманентний бан.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ До скарг", callback_data="admin_complaints")],
        ]),
    )
    # Сповіщаємо забаненого
    try:
        await bot.send_message(
            c["accused_tg"],
            "⛔ Вас <b>назавжди заблоковано</b> в надсиланні анонімних повідомлень.\n"
            "Причина: скарга від іншого користувача."
        )
    except Exception:
        pass


@dp.callback_query(lambda c: c.data and c.data.startswith("admin_timeout:"))
async def callback_admin_timeout(callback: types.CallbackQuery):
    if callback.from_user.id not in authorized_admins:
        await callback.answer("⛔ Немає доступу.", show_alert=True)
        return
    try:
        complaint_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Помилка.")
        return

    pending_admin_timeout[callback.from_user.id] = complaint_id
    await callback.answer()
    await callback.message.answer(
        "⏱ <b>Таймаут</b>\n\n"
        "Введіть тривалість у форматі:\n"
        "<code>30m</code> — 30 хвилин\n"
        "<code>2h</code>  — 2 години\n"
        "<code>7d</code>  — 7 днів\n\n"
        "Щоб скасувати — /stop"
    )


@dp.callback_query(lambda c: c.data and c.data.startswith("admin_dismiss:"))
async def callback_admin_dismiss(callback: types.CallbackQuery):
    if callback.from_user.id not in authorized_admins:
        await callback.answer("⛔ Немає доступу.", show_alert=True)
        return
    try:
        complaint_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Помилка.")
        return

    resolve_complaint(complaint_id, "dismissed", callback.from_user.id)
    await callback.answer("✅ Скаргу відхилено.")
    await callback.message.edit_text(
        f"✅ Скаргу #{complaint_id} відхилено.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ До скарг", callback_data="admin_complaints")],
        ]),
    )


# ======================
# ⚙️ ПЕРЕСИЛАННЯ
# ======================

async def forward_message(chat_id: int, message: types.Message, reply_markup=None,
                          reply_to_message_id: int | None = None) -> int | None:
    """Пересилає повідомлення анонімно. Повертає message_id надісланого повідомлення."""
    kwargs = {}
    if reply_to_message_id:
        kwargs["reply_to_message_id"] = reply_to_message_id
        kwargs["allow_sending_without_reply"] = True  # якщо оригінал видалено — надіслати без reply
    try:
        if message.text:
            sent = await bot.send_message(chat_id, message.text, reply_markup=reply_markup, **kwargs)
        else:
            sent = await bot.copy_message(
                chat_id=chat_id,
                from_chat_id=message.chat.id,
                message_id=message.message_id,
                reply_markup=reply_markup,
                **kwargs,
            )
        return sent.message_id if sent else None
    except Exception as e:
        logging.warning(f"forward_message error (retry without reply): {e}")
        # Якщо з reply не вийшло — надсилаємо без нього
        try:
            if message.text:
                sent = await bot.send_message(chat_id, message.text, reply_markup=reply_markup)
            else:
                sent = await bot.copy_message(
                    chat_id=chat_id,
                    from_chat_id=message.chat.id,
                    message_id=message.message_id,
                    reply_markup=reply_markup,
                )
            return sent.message_id if sent else None
        except Exception as e2:
            logging.error(f"forward_message fatal error: {e2}")
            return None


async def forward_media_group(chat_id: int, messages: list[types.Message], reply_markup=None):
    """Пересилає медіагрупу (альбом) як один альбом через copy_messages.
    Повертає список надісланих повідомлень (у тому ж порядку, що й вхідні)."""
    message_ids = [m.message_id for m in messages]
    from_chat_id = messages[0].chat.id
    # copy_messages надсилає одразу групою — зберігається як альбом
    sent = await bot.copy_messages(
        chat_id=chat_id,
        from_chat_id=from_chat_id,
        message_ids=message_ids,
    )
    # Якщо є markup — чіпляємо до останнього повідомлення альбому
    if reply_markup and sent:
        last_msg_id = sent[-1].message_id
        await bot.edit_message_reply_markup(
            chat_id=chat_id,
            message_id=last_msg_id,
            reply_markup=reply_markup,
        )
    return sent


async def _flush_media_group(
    media_group_id: str,
    author_db_id: int,
    author_tg_id: int,
    session_code: str,
    target_tg_id: int,
    target_db_id: int,
    anon_code: str,
):
    """Викликається через asyncio після затримки — відправляє зібраний альбом."""
    await asyncio.sleep(0.6)  # чекаємо поки Telegram надішле всі фото з групи

    entry = media_group_buffer.pop(media_group_id, None)
    if not entry:
        return

    messages = entry["messages"]
    if not messages:
        return

    admin_label_line = build_admin_label_line(author_db_id)
    markup = build_message_markup(session_code, author_db_id)

    if admin_label_line:
        await bot.send_message(
            target_tg_id,
            f"⚠️ <b>Увага від адміністрації:</b>{admin_label_line}"
        )

    sent_messages = await forward_media_group(target_tg_id, messages, reply_markup=markup)
    # Зберігаємо зв'язок для кожного фото в альбомі (підпис можна редагувати окремо)
    # Markup прикріплений лише до останнього повідомлення альбому
    if sent_messages:
        last_index = len(sent_messages) - 1
        for i, (orig, sent) in enumerate(zip(messages, sent_messages)):
            ct = get_content_type_of_message(orig) or "photo"
            save_message_link(
                author_tg_id, orig.message_id,
                target_tg_id, sent.message_id, ct, session_code,
                reply_markup=markup if i == last_index else None,
            )
    # Альбом рахуємо як одне повідомлення
    increment_sent_count(author_db_id)
    increment_received_count(target_db_id)
    close_visitor_session(author_db_id)

    write_again_markup = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="✉️ Написати ще раз",
            callback_data=f"write_again:{anon_code}",
        )
    ]])
    # Повідомляємо відправника (перше повідомлення групи)
    try:
        await bot.send_message(
            author_tg_id,
            f"✅ Альбом ({len(messages)} фото) надіслано анонімно.\n"
            "Якщо хочете написати ще — натисніть кнопку нижче.",
            reply_markup=write_again_markup,
        )
    except Exception:
        pass


# ======================
# ⚙️ РЕДАГУВАННЯ ПОВІДОМЛЕНЬ
# ======================

# Типи контенту, де можна редагувати підпис (caption)
CAPTION_EDITABLE_TYPES = {"photo", "video", "audio", "document", "animation", "voice"}


@dp.edited_message()
async def edited_message_handler(message: types.Message):
    """Коли відправник редагує своє повідомлення в Telegram —
    застосовуємо те саме редагування до копії, яку вже отримав інший учасник."""
    link = find_message_link(message.chat.id, message.message_id)
    if not link:
        # Немає зв'язку (повідомлення старе, не пересилалось, або вже не редагується)
        return

    ct = link["content_type"]

    # Відновлюємо ту саму inline-клавіатуру, яка була на копії (інакше edit її прибере)
    restored_markup = None
    if link["reply_markup_json"]:
        try:
            restored_markup = InlineKeyboardMarkup.model_validate(
                json.loads(link["reply_markup_json"])
            )
        except Exception:
            restored_markup = None

    try:
        if ct == "text":
            if not message.text:
                return
            await bot.edit_message_text(
                chat_id=link["recipient_chat_id"],
                message_id=link["recipient_msg_id"],
                text=message.text,
                reply_markup=restored_markup,
            )
        elif ct in CAPTION_EDITABLE_TYPES:
            await bot.edit_message_caption(
                chat_id=link["recipient_chat_id"],
                message_id=link["recipient_msg_id"],
                caption=message.caption,
                reply_markup=restored_markup,
            )
        else:
            # Стікери, кружки, локації, контакти — Telegram не дозволяє редагувати
            return
    except Exception as e:
        logging.info(f"Не вдалося відредагувати повідомлення-копію: {e}")
        return

    # Повідомляємо відправника, що редагування застосовано
    try:
        await bot.send_message(
            message.chat.id,
            "✏️ Повідомлення відредаговано. Отримувач побачить оновлений варіант.",
            reply_to_message_id=message.message_id,
        )
    except Exception:
        pass


# ======================
# ⚙️ ГОЛОВНИЙ FALLBACK ХЕНДЛЕР
# ======================

@dp.message()
async def fallback_handler(message: types.Message):
    author = create_or_update_user(message.from_user.id, message.from_user.username)

    # --- Техобслуговування: блокуємо всіх крім адмінів ---
    if maintenance_mode and message.from_user.id not in authorized_admins:
        await message.answer(
            "🔧 <b>Бот на технічному обслуговуванні.</b>\n\n"
            "Зачекайте трохи — скоро повернемось! ⏳"
        )
        return

    # --- Адмін: встановлення репутації вручну ---
    if message.from_user.id in pending_admin_rep_set:
        target_db_id = pending_admin_rep_set.pop(message.from_user.id)
        if not message.text or not message.text.strip().lstrip("+-").isdigit():
            await message.answer(
                "⚠️ Введіть ціле число (наприклад: <code>50</code>, <code>-10</code>, <code>0</code>).\n"
                "Скасувати — /stop"
            )
            pending_admin_rep_set[message.from_user.id] = target_db_id
            return
        new_rep = int(message.text.strip())
        admin_set_rep(target_db_id, new_rep)
        target_u = find_user_by_id(target_db_id)
        username_str = f"@{target_u['username']}" if target_u and target_u["username"] else f"db#{target_db_id}"
        emoji, level = get_rep_level(new_rep)
        admin_log(message.from_user.id, "set_rep", target_db_id, f"→ {new_rep}")
        await message.answer(
            f"✅ Репутацію встановлено!\n\n"
            f"Користувач: {username_str}\n"
            f"{emoji} {level} ({new_rep:+d})",
            reply_markup=build_admin_menu(),
        )
        return

    # --- Адмін: введення мітки або пошук ---
    if message.from_user.id in pending_admin_label:
        admin_target = pending_admin_label[message.from_user.id]

        # Режим пошуку користувача (admin_target == -1)
        if admin_target == -1:
            del pending_admin_label[message.from_user.id]
            query = (message.text or "").strip().lstrip("@")
            found_user = None

            # Пробуємо знайти по tg_id
            if query.isdigit():
                found_user = find_user_by_tg_id(int(query))

            # Або по username
            if not found_user:
                conn = get_db_connection()
                try:
                    found_user = conn.execute(
                        "SELECT * FROM users WHERE username = ?", (query,)
                    ).fetchone()
                finally:
                    conn.close()

            if not found_user:
                await message.answer(
                    "❌ Користувача не знайдено. Перевірте ID або @username.",
                    reply_markup=build_admin_menu(),
                )
                return

            rep = get_user_rep_by_id(found_user["id"])
            emoji_r, level_r = get_rep_level(rep)
            current_label = get_admin_label(found_user["id"])
            label_str = f"\n⚠️ Мітка: <i>{current_label}</i>" if current_label else "\nМітка: <i>немає</i>"
            username_str = f"@{found_user['username']}" if found_user['username'] else f"tg_id:{found_user['tg_id']}"
            ban_info = get_ban(found_user["id"])
            if ban_info:
                if ban_info["ban_type"] == "permanent":
                    ban_str = "\n🔨 Статус: <b>Заблокований назавжди</b>"
                else:
                    ban_str = f"\n⏱ Статус: <b>Таймаут до {str(ban_info['banned_until'])[:16]}</b>"
            else:
                ban_str = ""

            rows = [
                [InlineKeyboardButton(
                    text="✏️ Встановити мітку",
                    callback_data=f"admin_set_label:{found_user['id']}",
                )],
                [InlineKeyboardButton(
                    text="⭐ Змінити репутацію",
                    callback_data=f"admin_rep_set:{found_user['id']}",
                )],
            ]
            if current_label:
                rows.append([InlineKeyboardButton(
                    text="🗑 Зняти мітку",
                    callback_data=f"admin_clear_label:{found_user['id']}",
                )])
            if ban_info:
                rows.append([InlineKeyboardButton(
                    text="🔓 Розбанити",
                    callback_data=f"admin_unban:{found_user['id']}",
                )])
            rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back")])

            await message.answer(
                f"🔍 <b>Знайдено:</b> {username_str}\n"
                f"ID в БД: <code>{found_user['id']}</code>\n"
                f"Репутація: {emoji_r} {level_r} ({rep:+d})"
                f"{ban_str}"
                f"{label_str}",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            )
            return

        # Режим введення мітки (admin_target = user_db_id)
        del pending_admin_label[message.from_user.id]
        if not message.text:
            await message.answer("⚠️ Мітка має бути текстом.", reply_markup=build_admin_menu())
            return
        label_text = message.text.strip()[:200]
        set_admin_label(admin_target, label_text, message.from_user.id)
        await message.answer(
            f"✅ Мітку встановлено:\n⚠️ {label_text}",
            reply_markup=build_admin_menu(),
        )
        return

    # --- Адмін: введення строку таймауту ---
    if message.from_user.id in pending_admin_timeout:
        complaint_id = pending_admin_timeout.pop(message.from_user.id)
        if not message.text:
            await message.answer("⚠️ Введіть строк текстом (наприклад: 2h, 30m, 7d).",
                                 reply_markup=build_admin_menu())
            return
        delta = parse_duration(message.text.strip())
        if delta is None:
            await message.answer(
                "❌ Невірний формат. Приклади: <code>30m</code>, <code>2h</code>, <code>7d</code>\n"
                "Спробуйте ще раз або /stop для скасування."
            )
            pending_admin_timeout[message.from_user.id] = complaint_id
            return
        complaint = get_complaint_by_id(complaint_id)
        if not complaint:
            await message.answer("❌ Скарга не знайдена.", reply_markup=build_admin_menu())
            return
        until = datetime.utcnow() + delta
        ban_user(complaint["accused_id"], "timeout",
                 f"Таймаут за скаргою #{complaint_id}", message.from_user.id, until)
        resolve_complaint(complaint_id, "timeout", message.from_user.id)
        until_str = until.strftime("%d.%m.%Y %H:%M UTC")
        await message.answer(
            f"✅ Таймаут встановлено до <b>{until_str}</b>\n"
            f"Скаргу #{complaint_id} закрито.",
            reply_markup=build_admin_menu(),
        )
        # Повідомляємо забаненого
        try:
            await bot.send_message(
                complaint["accused_tg"],
                f"⏱ Вас тимчасово обмежено в надсиланні анонімних повідомлень до <b>{until_str}</b>.\n"
                "Причина: скарга від іншого користувача."
            )
        except Exception:
            pass
        return

    # --- Адмін: введення тексту новини ---
    if message.from_user.id in pending_admin_news:
        pending_admin_news.discard(message.from_user.id)
        if not message.text:
            await message.answer(
                "⚠️ Новина має бути текстом. Спробуйте ще раз через адмін-панель.",
                reply_markup=build_admin_menu(),
            )
            return
        news_text = message.text.strip()
        all_tg_ids = get_all_user_tg_ids()

        preview_markup = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Надіслати всім", callback_data="admin_news_confirm"),
                InlineKeyboardButton(text="❌ Скасувати", callback_data="admin_news_cancel"),
            ]
        ])
        # Зберігаємо текст тимчасово в пам'яті
        pending_admin_news_text[message.from_user.id] = news_text
        await message.answer(
            f"📢 <b>Попередній перегляд новини:</b>\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📣 <b>Новини бота</b>\n\n"
            f"{news_text}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"Отримувачів: <b>{len(all_tg_ids)}</b>",
            reply_markup=preview_markup,
        )
        return

    # --- Привітання ---
    if message.from_user.id in pending_greeting_input:
        pending_greeting_input.discard(message.from_user.id)
        if not message.text:
            await message.answer("⚠️ Привітання має бути текстом. Спробуйте ще раз через /settings.")
            return
        text = message.text.strip()[:200]
        set_user_greeting(author["id"], text)
        await message.answer(
            f"✅ Привітання збережено:\n\n💬 {text}\n\n"
            "Тепер відвідувачі бачитимуть його при переході за вашим посиланням."
        )
        return

    pending = get_reply_pending(author["id"])

    # --- Режим відповіді (target відповідає visitor'у) ---
    # Відповідь надсилається ЛИШЕ якщо користувач явно натиснув кнопку "Відповісти"
    if pending and message.from_user.id in active_reply_mode:
        session = find_session_by_code(pending["session_code"])
        if session:
            visitor = find_user_by_id(session["visitor_id"])
            if visitor:
                # Відповідаємо на оригінальне повідомлення відвідувача (свайп у нього)
                visitor_msg_id = pending["visitor_msg_id"] if pending["visitor_msg_id"] else None
                sent_id = await forward_message(visitor["tg_id"], message,
                                                reply_to_message_id=visitor_msg_id)
                # Оновлюємо статистику: автор відповіді надіслав, відвідувач отримав
                increment_sent_count(author["id"])
                increment_received_count(visitor["id"])
                if sent_id:
                    ct = get_content_type_of_message(message) or "text"
                    # Зберігаємо зв'язок: автор=target (message.message_id в його чаті),
                    # recipient=відвідувач (sent_id — ID що відвідувач бачить).
                    # Але для свайпу відвідувача нам треба знайти ID в чаті target'а,
                    # яке ВІДПОВІДАЄ цьому sent_id. Target бачив повідомлення відвідувача
                    # як pending["target_msg_id"]. Тому зберігаємо author_msg_id = target_msg_id.
                    target_msg_id_in_chat = pending["target_msg_id"] if pending["target_msg_id"] else message.message_id
                    save_message_link(
                        message.from_user.id, target_msg_id_in_chat,
                        visitor["tg_id"], sent_id, ct, pending["session_code"],
                    )
                # Зберігаємо ID щоб відвідувач міг свайпнути цю відповідь
                if sent_id:
                    update_visitor_inbox_msg_id(pending["session_code"], sent_id)
                active_reply_mode.discard(message.from_user.id)
                await message.answer("✅ Відповідь відправлена анонімно.")
                clear_reply_pending(author["id"])
                return
        active_reply_mode.discard(message.from_user.id)
        clear_reply_pending(author["id"])
        await message.answer("⚠️ Сесія вже закрита. Відповідь не відправлена.")
        return

    # --- Відвідувач надсилає повідомлення ---
    session = find_active_session_by_visitor(author["id"])
    if session:
        target = find_user_by_id(session["target_id"])
        if target:
            # Перевірка бану
            banned, ban_msg = is_banned(author["id"])
            if banned:
                await message.answer(ban_msg)
                return

            ct = get_content_type_of_message(message)
            if ct and not is_content_allowed(target["id"], ct):
                meta = CONTENT_TYPES.get(ct, {})
                label = f"{meta.get('emoji', '')} {meta.get('label', ct)}"
                await message.answer(
                    f"⛔ Цей користувач не приймає: <b>{label}</b>\n"
                    "Спробуйте надіслати інший тип повідомлення."
                )
                return

            # Формуємо markup з репутацією і міткою адміна
            admin_label_line = build_admin_label_line(author["id"])
            markup = build_message_markup(session["session_code"], author["id"])

            # --- Медіагрупа (альбом з кількох фото/відео) ---
            if message.media_group_id:
                mgid = message.media_group_id
                if mgid not in media_group_buffer:
                    media_group_buffer[mgid] = {"messages": [], "task": None}
                media_group_buffer[mgid]["messages"].append(message)

                # Скасовуємо попередній таймер і запускаємо новий
                old_task = media_group_buffer[mgid]["task"]
                if old_task and not old_task.done():
                    old_task.cancel()

                target_user = find_user_by_id(session["target_id"])
                task = asyncio.create_task(_flush_media_group(
                    mgid,
                    author["id"],
                    message.from_user.id,
                    session["session_code"],
                    target["tg_id"],
                    target["id"],
                    target_user["anon_code"],
                ))
                media_group_buffer[mgid]["task"] = task
                return  # не закриваємо сесію тут — _flush_media_group зробить це

            # Якщо є адмін-мітка — надсилаємо окреме попередження перед повідомленням
            if admin_label_line:
                await bot.send_message(
                    target["tg_id"],
                    f"⚠️ <b>Увага від адміністрації:</b>{admin_label_line}"
                )

            # Надсилаємо повідомлення отримувачу, запам'ятовуємо ID
            # Якщо відвідувач свайпнув відповідь отримувача — передаємо reply у чат отримувача
            reply_in_target_chat = None
            if message.reply_to_message:
                # Відвідувач свайпнув повідомлення яке надійшло від target'а.
                # В message_links воно записане як:
                #   author_chat_id = target_tg_id  (хто надіслав)
                #   author_msg_id  = ID у target'а (що нам потрібно для reply)
                #   recipient_chat_id = visitor_tg_id
                #   recipient_msg_id  = ID у відвідувача (свайпнуте)
                conn = get_db_connection()
                try:
                    ml_row = conn.execute(
                        """SELECT author_msg_id FROM message_links
                           WHERE recipient_chat_id = ? AND recipient_msg_id = ?""",
                        (message.from_user.id, message.reply_to_message.message_id),
                    ).fetchone()
                    if ml_row:
                        reply_in_target_chat = ml_row["author_msg_id"]
                finally:
                    conn.close()

            sent_msg_id = await forward_message(target["tg_id"], message, reply_markup=markup,
                                                reply_to_message_id=reply_in_target_chat)

            # Оновлюємо статистику: відправник надіслав, отримувач отримав
            increment_sent_count(author["id"])
            increment_received_count(target["id"])

            # Зберігаємо зв'язок для можливості редагування цього повідомлення
            if sent_msg_id:
                save_message_link(
                    message.from_user.id, message.message_id,
                    target["tg_id"], sent_msg_id, ct or "text", session["session_code"],
                    reply_markup=markup,
                )

            # Зберігаємо ID для свайп-відповіді отримувача
            set_reply_pending(
                target["id"],
                session["session_code"],
                target_msg_id=sent_msg_id,
                visitor_msg_id=message.message_id,
            )

            # Закрити сесію після одного повідомлення
            close_visitor_session(author["id"])

            target_user = find_user_by_id(session["target_id"])
            write_again_markup = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text="✉️ Написати ще раз",
                    callback_data=f"write_again:{target_user['anon_code']}",
                )
            ]])
            await message.answer(
                "✅ Повідомлення надіслано анонімно.\n"
                "Якщо хочете написати ще — натисніть кнопку нижче.",
                reply_markup=write_again_markup,
            )
            return

    await message.answer(
        "ℹ️ Немає активного чату. Використайте /start, щоб отримати своє анонімне посилання.",
        reply_markup=build_main_menu(),
    )


# ======================
# ⚙️ ЗАПУСК
# ======================

async def main():
    global BOT_USERNAME
    init_db()
    # Відновлюємо авторизованих адмінів з БД після перезапуску
    for tg_id in db_get_all_admin_tg_ids():
        authorized_admins.add(tg_id)
    logging.info(f"Завантажено адмінів з БД: {len(authorized_admins)}")
    me = await bot.get_me()
    BOT_USERNAME = me.username
    logging.info(f"Бот запущено: @{BOT_USERNAME}")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Бот зупинено")