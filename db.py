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
    "channel_target": "",       # چت آیدی/یوزرنیم مقصد ارسال پست (باید ربات ادمین کانال باشه)
    "channel_display": "",      # متنی که به جای "ایدی چنل" داخل پیام قرار میگیره
    "upload_btn_text": "آپلود فایل",
    "single_btn_text": "تکی",
    "back_btn_text": "بازگشت به منو اصلی",
    "session_string": "",       # با پنل داخل ربات (🔑 ورود سشن جدید) پر میشه
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
