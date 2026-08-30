import sqlite3
import os

# روی ریلوی حتما این رو با یه Volume مقداردهی کن (مثلا /data/data.sqlite3)
# وگرنه سر هر ری‌دیپلوی، سشن و تنظیمات پاک میشن.
DB_PATH = os.environ.get(
    "DB_PATH", os.path.join(os.path.dirname(__file__), "data.sqlite3")
)

DEFAULTS = {
    "bot_x_username": "",
    "backup_bot_username": "",
    "trigger_word": "مشاهده",   # کلمه‌ای که توی پیام تبدیل به لینک بکاپ میشه
    "session_string": "",       # با پنل داخل ربات (🔑 ورود سشن جدید) پر میشه
    "watch_interval_seconds": "3600",  # حداقل فاصله بین دو دور دانلود از چنل‌های مانیتور
    "watch_max_per_day": "5",          # حداکثر تعداد فایل در ۲۴ ساعت از چنل‌های مانیتور
    "post_template": "",               # قالب پیام برای پست خودکار (شامل «ایدی چنل» و کلمه‌ی تریگر)
}


def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(bootstrap_admin_ids=None):
    conn = _conn()
    cur = conn.cursor()
    cur.execute(
        "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)"
    )
    cur.execute(
        "CREATE TABLE IF NOT EXISTS admins (user_id INTEGER PRIMARY KEY)"
    )
    cur.execute(
        "CREATE TABLE IF NOT EXISTS last_link (user_id INTEGER PRIMARY KEY, link TEXT)"
    )
    cur.execute(
        "CREATE TABLE IF NOT EXISTS channels ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "target TEXT NOT NULL, "
        "display TEXT)"
    )
    cur.execute(
        "CREATE TABLE IF NOT EXISTS watched_channels ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "target TEXT NOT NULL, "
        "title TEXT, "
        "added_at INTEGER)"
    )
    cur.execute(
        "CREATE TABLE IF NOT EXISTS downloaded ("
        "file_unique_id TEXT PRIMARY KEY, "
        "chat_id TEXT, "
        "downloaded_at INTEGER)"
    )
    conn.commit()

    for k, v in DEFAULTS.items():
        cur.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v)
        )
    conn.commit()

    if bootstrap_admin_ids:
        for uid in bootstrap_admin_ids:
            cur.execute(
                "INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (int(uid),)
            )
    conn.commit()
    conn.close()


def get_setting(key):
    conn = _conn()
    row = conn.execute(
        "SELECT value FROM settings WHERE key = ?", (key,)
    ).fetchone()
    conn.close()
    return row["value"] if row else None


def get_all_settings():
    conn = _conn()
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    conn.close()
    return {r["key"]: r["value"] for r in rows}


def set_setting(key, value):
    conn = _conn()
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()
    conn.close()


def is_admin(user_id):
    conn = _conn()
    row = conn.execute(
        "SELECT 1 FROM admins WHERE user_id = ?", (user_id,)
    ).fetchone()
    conn.close()
    return row is not None


def add_admin(user_id):
    conn = _conn()
    conn.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (user_id,))
    conn.commit()
    conn.close()


def remove_admin(user_id):
    conn = _conn()
    conn.execute("DELETE FROM admins WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


def list_admins():
    conn = _conn()
    rows = conn.execute("SELECT user_id FROM admins").fetchall()
    conn.close()
    return [r["user_id"] for r in rows]


def set_last_link(user_id, link):
    conn = _conn()
    conn.execute(
        "INSERT INTO last_link (user_id, link) VALUES (?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET link = excluded.link",
        (user_id, link),
    )
    conn.commit()
    conn.close()


def get_last_link(user_id):
    conn = _conn()
    row = conn.execute(
        "SELECT link FROM last_link WHERE user_id = ?", (user_id,)
    ).fetchone()
    conn.close()
    return row["link"] if row else None


def add_channel(target, display):
    conn = _conn()
    cur = conn.execute(
        "INSERT INTO channels (target, display) VALUES (?, ?)", (target, display)
    )
    conn.commit()
    channel_id = cur.lastrowid
    conn.close()
    return channel_id


def list_channels():
    conn = _conn()
    rows = conn.execute("SELECT id, target, display FROM channels ORDER BY id").fetchall()
    conn.close()
    return [{"id": r["id"], "target": r["target"], "display": r["display"]} for r in rows]


def get_channel(channel_id):
    conn = _conn()
    row = conn.execute(
        "SELECT id, target, display FROM channels WHERE id = ?", (channel_id,)
    ).fetchone()
    conn.close()
    return {"id": row["id"], "target": row["target"], "display": row["display"]} if row else None


def remove_channel(channel_id):
    conn = _conn()
    conn.execute("DELETE FROM channels WHERE id = ?", (channel_id,))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------- watched channels (source)
def add_watched_channel(target, title):
    conn = _conn()
    cur = conn.execute(
        "INSERT INTO watched_channels (target, title, added_at) VALUES (?, ?, strftime('%s','now'))",
        (str(target), title),
    )
    conn.commit()
    channel_id = cur.lastrowid
    conn.close()
    return channel_id


def list_watched_channels():
    conn = _conn()
    rows = conn.execute("SELECT id, target, title FROM watched_channels ORDER BY id").fetchall()
    conn.close()
    return [{"id": r["id"], "target": r["target"], "title": r["title"]} for r in rows]


def remove_watched_channel(channel_id):
    conn = _conn()
    conn.execute("DELETE FROM watched_channels WHERE id = ?", (channel_id,))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------- download dedup / rate limit
def already_downloaded(file_unique_id):
    conn = _conn()
    row = conn.execute(
        "SELECT 1 FROM downloaded WHERE file_unique_id = ?", (file_unique_id,)
    ).fetchone()
    conn.close()
    return row is not None


def mark_downloaded(file_unique_id, chat_id):
    conn = _conn()
    conn.execute(
        "INSERT OR IGNORE INTO downloaded (file_unique_id, chat_id, downloaded_at) "
        "VALUES (?, ?, strftime('%s','now'))",
        (file_unique_id, str(chat_id)),
    )
    conn.commit()
    conn.close()


def downloads_in_last_24h():
    conn = _conn()
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM downloaded WHERE downloaded_at >= strftime('%s','now') - 86400"
    ).fetchone()
    conn.close()
    return row["c"]


def last_download_time():
    conn = _conn()
    row = conn.execute("SELECT MAX(downloaded_at) AS m FROM downloaded").fetchone()
    conn.close()
    return row["m"] or 0
