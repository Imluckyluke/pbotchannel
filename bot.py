import asyncio
import os
import re
import time
import logging

from dotenv import load_dotenv
from telethon import TelegramClient, events, Button
from telethon.sessions import StringSession
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest
from telethon.errors import (
    RPCError,
    FloodWaitError,
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    PasswordHashInvalidError,
    UserAlreadyParticipantError,
    InviteHashExpiredError,
)

import db

load_dotenv()

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
ENV_SESSION_STRING = os.environ.get("SESSION_STRING", "")  # فقط برای اولین بوت اختیاریه
ADMIN_IDS = [
    int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()
]

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("relay-bot")

LINK_RE = re.compile(r"https?://\S+")
INVITE_LINK_RE = re.compile(r"(?:https?://)?t\.me/(?:joinchat/|\+)?([A-Za-z0-9_]+)")
DOWNLOAD_DIR = os.path.join(os.path.dirname(__file__), "downloads")

db.init_db(bootstrap_admin_ids=ADMIN_IDS)

bot = TelegramClient(StringSession(), API_ID, API_HASH)
user = TelegramClient(StringSession(), API_ID, API_HASH)  # تا لاگین نشه وصل نیست

session_lock = asyncio.Lock()

PENDING = {}          # user_id -> کلید تنظیمی که منتظر مقدار متنیشیم
LOGIN_SESSIONS = {}    # user_id -> {client, phone, phone_code_hash, code}

# تنظیمات پایه‌ی جریان کار (ربات اول -> ربات دوم -> کلمه‌ی تریگر)
CORE_SETTINGS = [
    ("bot1_username", "🤖 ربات اول"),
    ("bot2_username", "🤖 ربات دوم"),
    ("trigger_word", "🔗 کلمه‌ی تبدیل به لینک"),
]

# تنظیمات مربوط به بررسی خودکار کانال‌های مبدا
WATCH_SETTINGS = [
    ("watch_interval_minutes", "⏱ فاصله زمانی بین هر پست (دقیقه)"),
    ("watch_max_per_day", "🔢 تعداد پست روزانه"),
    ("post_template", "📝 قالب جایگزین (بدون کپشن مبدا)"),
]

SETTINGS_LABELS = {key: label for key, label in CORE_SETTINGS + WATCH_SETTINGS}
NUMERIC_SETTINGS = {"watch_interval_minutes", "watch_max_per_day"}

TEMP_DEST_CHANNEL = {}    # user_id -> target موقت هنگام افزودن کانال مقصد جدید
PENDING_MSG = {}          # user_id -> (chat_id, message_id) پیام پنلی که باید ادیت بشه

SESSION_ALERT_SENT = False   # تا وقتی سشن خرابه دوباره و دوباره به ادمین‌ها پیام نده


# ---------------------------------------------------------------- helpers
def is_admin(user_id: int) -> bool:
    return db.is_admin(user_id)


async def send_or_edit(admin_id: int, event, text: str, buttons=None, parse_mode=None):
    """اگه پیام پنلی قبلی داریم همونو ادیت میکنه، وگرنه پیام جدید میفرسته."""
    loc = PENDING_MSG.get(admin_id)
    if loc:
        try:
            return await bot.edit_message(loc[0], loc[1], text, buttons=buttons, parse_mode=parse_mode)
        except Exception:
            pass
    return await event.reply(text, buttons=buttons, parse_mode=parse_mode)


def extract_link(text: str) -> str:
    if not text:
        raise RuntimeError("پیام خالی بود، لینکی توش نبود")
    matches = LINK_RE.findall(text)
    if not matches:
        raise RuntimeError(f"لینکی توی پاسخ پیدا نشد:\n{text}")
    # همیشه لینک واقعی (نهایی) ته پیامه؛ اگه لینک دیگه‌ای هم قبلش باشه
    # (تبلیغ، کانال و غیره) نباید اونو به اشتباه برداریم.
    return matches[-1]


async def reconnect_user_client(session_string: str):
    """کلاینت سشن (یوزربات) رو با استرینگ جدید وصل/جایگزین میکنه."""
    global user
    try:
        if user.is_connected():
            await user.disconnect()
    except Exception:
        pass
    user = TelegramClient(StringSession(session_string), API_ID, API_HASH)
    await user.start()


def session_connected() -> bool:
    return user.is_connected()


async def session_health_ok() -> bool:
    """چک واقعی اینکه سشن هنوز متصل و مجازه، نه فقط وصل بودن سوکت."""
    if not user.is_connected():
        return False
    try:
        return await user.is_user_authorized()
    except Exception:
        return False


async def call_with_flood_retry(coro_factory, *, context: str, max_inline_wait: int = 300):
    """coro_factory: تابع بدون آرگومان که هر بار صداش کنیم یه کوروتین جدید بده.
    اگه FloodWaitError بگیریم و مدت صبر کوتاه باشه (<= max_inline_wait)، صبر میکنیم
    و یبار دیگه امتحان میکنیم؛ وگرنه به ادمین‌ها خبر میدیم و خطا رو پرتاب میکنیم."""
    try:
        return await coro_factory()
    except FloodWaitError as e:
        wait_s = e.seconds
        log.warning("FloodWaitError در «%s»: باید %s ثانیه صبر کنیم", context, wait_s)
        if wait_s <= max_inline_wait:
            await asyncio.sleep(wait_s + 1)
            return await coro_factory()
        await notify_admins(
            f"⚠️ محدودیت فلود تلگرام روی «{context}»: باید {wait_s} ثانیه "
            f"(~{wait_s // 60} دقیقه) صبر کنیم. این عملیات فعلاً رد شد."
        )
        raise


async def join_from_identifier(identifier: str) -> bool:
    """identifier: لینک t.me، یوزرنیم @، یا آیدی عددی کانالی که سشن از قبل عضوشه."""
    m = INVITE_LINK_RE.search(identifier)
    try:
        if m and ("/+" in identifier or "joinchat" in identifier):
            await call_with_flood_retry(
                lambda: user(ImportChatInviteRequest(m.group(1))),
                context=f"عضویت در {identifier}",
            )
        elif m:
            await call_with_flood_retry(
                lambda: user(JoinChannelRequest(m.group(1))),
                context=f"عضویت در {identifier}",
            )
        elif identifier.startswith("@"):
            await call_with_flood_retry(
                lambda: user(JoinChannelRequest(identifier)),
                context=f"عضویت در {identifier}",
            )
        else:
            return True  # آیدی عددی: فرض میکنیم از قبل عضوه
        return True
    except UserAlreadyParticipantError:
        return True
    except InviteHashExpiredError:
        return False
    except Exception as e:
        log.warning("join failed for %s: %s", identifier, e)
        return False


# ---------------------------------------------------------------- panel UI
def settings_row(key: str, label: str):
    current = db.get_setting(key)
    val = current if current else "تنظیم‌نشده"
    style = "success" if current else "danger"
    return [Button.inline(f"{label}: {val}", data=f"set:{key}", style=style)]


def main_menu_buttons():
    rows = []

    # بخش ۱: تنظیمات پایه‌ی جریان کار
    for key, label in CORE_SETTINGS:
        rows.append(settings_row(key, label))

    # بخش ۲: ورود سشن (یوزربات)
    connected = session_connected()
    status = "متصل ✅" if connected else "قطع ❌"
    rows.append([Button.inline(f"🔑 ورود با شماره ({status})", data="login:start", style="success" if connected else "danger")])
    rows.append([Button.inline("📋 ورود مستقیم با Session String", data="login:string", style="primary")])

    # بخش ۳: مدیریت کانال‌ها
    rows.append([Button.inline(f"📢 کانال‌های مقصد ({len(db.list_dest_channels())})", data="menu:destchannels", style="primary")])
    rows.append([Button.inline(f"📡 کانال‌های مبدا - مانیتور ({len(db.list_src_channels())})", data="menu:srcchannels", style="primary")])

    # بخش ۴: تنظیمات بررسی خودکار کانال‌های مبدا
    for key, label in WATCH_SETTINGS:
        rows.append(settings_row(key, label))

    paused = db.get_setting("auto_paused") == "1"
    pause_label = "▶️ فعال‌سازی ارسال خودکار" if paused else "⏸ توقف ارسال خودکار"
    rows.append([Button.inline(pause_label, data="menu:togglepause", style="danger" if paused else "success")])

    # بخش ۵: مدیریت و اطلاعات
    rows.append([Button.inline(f"👤 مدیریت ادمین‌ها ({len(db.list_admins())})", data="menu:admins", style="primary")])
    rows.append([Button.inline("ℹ️ نمایش کامل تنظیمات", data="menu:show")])
    rows.append([Button.inline("❌ بستن", data="menu:close", style="danger")])
    return rows


def admins_menu_buttons():
    rows = [[Button.inline(f"❌ حذف {a}", data=f"admin:rm:{a}", style="danger")] for a in db.list_admins()]
    rows.append([Button.inline("➕ افزودن ادمین", data="admin:add", style="success")])
    rows.append([Button.inline("🔙 بازگشت", data="menu:main")])
    return rows


def dest_channels_menu_buttons():
    rows = []
    for c in db.list_dest_channels():
        name = c["display"] or c["target"]
        status = "فعال ✅" if c["enabled"] else "غیرفعال ⛔"
        rows.append([
            Button.inline(f"{name} — {status}", data=f"destchannel:toggle:{c['id']}",
                          style="success" if c["enabled"] else "danger"),
            Button.inline("❌ حذف", data=f"destchannel:rm:{c['id']}", style="danger"),
        ])
    rows.append([Button.inline("➕ افزودن کانال مقصد", data="destchannel:add", style="success")])
    rows.append([Button.inline("🔙 بازگشت", data="menu:main")])
    return rows


def src_channels_menu_buttons():
    rows = [
        [Button.inline(f"❌ حذف {c['title'] or c['target']}", data=f"srcchannel:rm:{c['id']}", style="danger")]
        for c in db.list_src_channels()
    ]
    rows.append([Button.inline("➕ افزودن کانال مبدا", data="srcchannel:add", style="success")])
    rows.append([Button.inline("🔙 بازگشت", data="menu:main")])
    return rows


def code_keypad(code_so_far: str):
    rows, row = [], []
    for d in ["1", "2", "3", "4", "5", "6", "7", "8", "9"]:
        row.append(Button.inline(d, data=f"login:digit:{d}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    rows.append([
        Button.inline("0", data="login:digit:0"),
        Button.inline("⌫ پاک کردن", data="login:back", style="danger"),
        Button.inline("✅ تایید", data="login:submit", style="success"),
    ])
    rows.append([Button.inline("انصراف", data="login:cancel", style="danger")])
    return rows


def masked_code(code: str) -> str:
    return " ".join(list(code)) if code else "—"


@bot.on(events.NewMessage(pattern="/start"))
async def start_handler(event):
    if not is_admin(event.sender_id):
        await event.reply("دسترسی نداری.")
        return
    await event.reply(
        "آماده‌ام. یه فایل بفرست تا لینک نهایی بگیرم، یا از پنل زیر همه چیزو تنظیم کن:",
        buttons=main_menu_buttons(),
    )


@bot.on(events.NewMessage(pattern="/panel"))
async def panel_handler(event):
    if not is_admin(event.sender_id):
        return
    await event.reply("پنل تنظیمات:", buttons=main_menu_buttons())


# ---------------------------------------------------------------- login wizard
async def start_login_phone(event, phone: str):
    admin_id = event.sender_id
    try:
        temp_client = TelegramClient(StringSession(), API_ID, API_HASH)
        await temp_client.connect()
        sent = await temp_client.send_code_request(phone)
    except Exception as e:
        await send_or_edit(admin_id, event, f"خطا در ارسال کد: {e}", buttons=main_menu_buttons())
        PENDING_MSG.pop(admin_id, None)
        return

    LOGIN_SESSIONS[admin_id] = {
        "client": temp_client,
        "phone": phone,
        "phone_code_hash": sent.phone_code_hash,
        "code": "",
    }
    await send_or_edit(
        admin_id, event,
        f"کد به {phone} ارسال شد.\nکد وارد شده: —\n\nبا دکمه‌های زیر وارد کن:",
        buttons=code_keypad(""),
    )


async def finish_login(edit_target, admin_id: int):
    sess = LOGIN_SESSIONS.pop(admin_id, None)
    if not sess:
        return
    temp_client = sess["client"]
    session_string = temp_client.session.save()
    db.set_setting("session_string", session_string)
    await temp_client.disconnect()
    await reconnect_user_client(session_string)

    text = f"ورود موفق بود، سشن ذخیره و وصل شد.\n\nSession String:\n`{session_string}`"
    await send_or_edit(admin_id, edit_target, text, buttons=main_menu_buttons(), parse_mode="markdown")
    PENDING_MSG.pop(admin_id, None)


@bot.on(events.CallbackQuery())
async def callback_handler(event):
    if not is_admin(event.sender_id):
        await event.answer("دسترسی نداری", alert=True)
        return

    data = event.data.decode()
    admin_id = event.sender_id

    if data == "menu:main":
        PENDING.pop(admin_id, None)
        PENDING_MSG.pop(admin_id, None)
        await event.edit("پنل تنظیمات:", buttons=main_menu_buttons())
        return

    if data == "menu:togglepause":
        paused = db.get_setting("auto_paused") == "1"
        db.set_setting("auto_paused", "0" if paused else "1")
        await event.edit("پنل تنظیمات:", buttons=main_menu_buttons())
        return

    if data == "menu:close":
        PENDING.pop(admin_id, None)
        PENDING_MSG.pop(admin_id, None)
        await event.edit("بسته شد. برای باز کردن دوباره /panel رو بزن.")
        return

    if data == "menu:show":
        cfg = db.get_all_settings()
        lines = []
        for k, v in cfg.items():
            if k == "session_string":
                continue
            lines.append(f"{SETTINGS_LABELS.get(k, k)}: {v or '—'}")
        lines.append(f"وضعیت سشن: {'متصل' if session_connected() else 'قطع'}")
        lines.append(f"ارسال خودکار: {'⏸ متوقف' if db.get_setting('auto_paused') == '1' else '✅ فعال'}")
        lines.append(f"ادمین‌ها: {db.list_admins()}")
        dest = db.list_dest_channels()
        if dest:
            lines.append("کانال‌های مقصد: " + ", ".join(
                f"{c['display'] or c['target']} ({'فعال' if c['enabled'] else 'غیرفعال'})" for c in dest
            ))
        else:
            lines.append("کانال‌های مقصد: —")
        src = db.list_src_channels()
        if src:
            lines.append("کانال‌های مبدا (مانیتور): " + ", ".join(c["title"] or c["target"] for c in src))
        else:
            lines.append("کانال‌های مبدا (مانیتور): —")
        await event.edit("\n".join(lines), buttons=[[Button.inline("🔙 بازگشت", data="menu:main")]])
        return

    if data == "menu:destchannels":
        PENDING.pop(admin_id, None)
        PENDING_MSG.pop(admin_id, None)
        await event.edit("کانال‌های مقصد (جایی که پست نهایی گذاشته میشه):", buttons=dest_channels_menu_buttons())
        return

    if data == "destchannel:add":
        PENDING[admin_id] = "add_dest_channel_target"
        PENDING_MSG[admin_id] = (event.chat_id, event.message_id)
        await event.edit(
            "آیدی عددی یا یوزرنیم کانال مقصد (مثلا @mychannel) رو بفرست:",
            buttons=[[Button.inline("انصراف", data="menu:destchannels", style="danger")]],
        )
        return

    if data.startswith("destchannel:rm:"):
        cid = int(data.split(":")[2])
        db.remove_dest_channel(cid)
        await event.edit("کانال مقصد حذف شد.\n\nکانال‌های مقصد:", buttons=dest_channels_menu_buttons())
        return

    if data.startswith("destchannel:toggle:"):
        cid = int(data.split(":")[2])
        new_state = db.toggle_dest_channel(cid)
        if new_state is None:
            await event.answer("این کانال دیگه وجود نداره.", alert=True)
            return
        await event.edit(
            "کانال‌های مقصد (جایی که پست نهایی گذاشته میشه):",
            buttons=dest_channels_menu_buttons(),
        )
        return

    if data == "menu:srcchannels":
        PENDING.pop(admin_id, None)
        PENDING_MSG.pop(admin_id, None)
        await event.edit("کانال‌های مبدا (که ازشون فایل جدید گرفته میشه):", buttons=src_channels_menu_buttons())
        return

    if data == "srcchannel:add":
        PENDING[admin_id] = "add_src_channel"
        PENDING_MSG[admin_id] = (event.chat_id, event.message_id)
        await event.edit(
            "لینک دعوت، یوزرنیم (@channel) یا آیدی عددی کانال مبدا رو بفرست:",
            buttons=[[Button.inline("انصراف", data="menu:srcchannels", style="danger")]],
        )
        return

    if data.startswith("srcchannel:rm:"):
        cid = int(data.split(":")[2])
        db.remove_src_channel(cid)
        await event.edit("کانال مبدا حذف شد.\n\nکانال‌های مبدا:", buttons=src_channels_menu_buttons())
        return

    if data == "menu:admins":
        PENDING.pop(admin_id, None)
        PENDING_MSG.pop(admin_id, None)
        await event.edit("مدیریت ادمین‌ها:", buttons=admins_menu_buttons())
        return

    if data.startswith("admin:rm:"):
        uid = int(data.split(":")[2])
        db.remove_admin(uid)
        await event.edit(f"ادمین {uid} حذف شد.\n\nمدیریت ادمین‌ها:", buttons=admins_menu_buttons())
        return

    if data == "admin:add":
        PENDING[admin_id] = "add_admin"
        PENDING_MSG[admin_id] = (event.chat_id, event.message_id)
        await event.edit(
            "آیدی عددی ادمین جدید رو بفرست:",
            buttons=[[Button.inline("انصراف", data="menu:admins", style="danger")]],
        )
        return

    if data.startswith("set:"):
        key = data.split(":", 1)[1]
        PENDING[admin_id] = key
        PENDING_MSG[admin_id] = (event.chat_id, event.message_id)
        current = db.get_setting(key)
        label = SETTINGS_LABELS.get(key, key)
        await event.edit(
            f"{label}\nمقدار فعلی: {current or '—'}\n\nمقدار جدید رو بفرست:",
            buttons=[[Button.inline("انصراف", data="menu:main", style="danger")]],
        )
        return

    if data.startswith("login:"):
        action = data.split(":", 1)[1]

        if action == "start":
            LOGIN_SESSIONS.pop(admin_id, None)
            PENDING[admin_id] = "login_phone"
            PENDING_MSG[admin_id] = (event.chat_id, event.message_id)
            await event.edit(
                "شماره تلفن اکانت سشن رو با فرمت بین‌المللی بفرست (مثلا +989123456789):",
                buttons=[[Button.inline("انصراف", data="menu:main", style="danger")]],
            )
            return

        if action == "string":
            LOGIN_SESSIONS.pop(admin_id, None)
            PENDING[admin_id] = "login_session_string"
            PENDING_MSG[admin_id] = (event.chat_id, event.message_id)
            await event.edit(
                "استرینگ سشن (Session String) رو بفرست:",
                buttons=[[Button.inline("انصراف", data="menu:main", style="danger")]],
            )
            return

        if action == "cancel":
            PENDING.pop(admin_id, None)
            PENDING_MSG.pop(admin_id, None)
            sess = LOGIN_SESSIONS.pop(admin_id, None)
            if sess:
                try:
                    await sess["client"].disconnect()
                except Exception:
                    pass
            await event.edit("لغو شد.", buttons=main_menu_buttons())
            return

        sess = LOGIN_SESSIONS.get(admin_id)
        if not sess:
            await event.answer("جلسه‌ی ورود پیدا نشد، دوباره از پنل شروع کن.", alert=True)
            return

        if action.startswith("digit:"):
            sess["code"] += action.split(":")[1]
            await event.edit(
                f"کد به {sess['phone']} ارسال شد.\nکد وارد شده: {masked_code(sess['code'])}\n\nبا دکمه‌های زیر وارد کن:",
                buttons=code_keypad(sess["code"]),
            )
            return

        if action == "back":
            sess["code"] = sess["code"][:-1]
            await event.edit(
                f"کد به {sess['phone']} ارسال شد.\nکد وارد شده: {masked_code(sess['code'])}\n\nبا دکمه‌های زیر وارد کن:",
                buttons=code_keypad(sess["code"]),
            )
            return

        if action == "submit":
            code = sess["code"]
            if not code:
                await event.answer("هنوز کدی وارد نکردی", alert=True)
                return
            try:
                await sess["client"].sign_in(
                    phone=sess["phone"], code=code, phone_code_hash=sess["phone_code_hash"]
                )
            except SessionPasswordNeededError:
                PENDING[admin_id] = "login_password"
                await event.edit(
                    "این اکانت تایید دو مرحله‌ای داره.\n"
                    "رمز (2FA) رو به صورت پیام متنی بفرست (بعد از خوندنش پیامت پاک میشه):",
                    buttons=[[Button.inline("انصراف", data="login:cancel", style="danger")]],
                )
                return
            except (PhoneCodeInvalidError, PhoneCodeExpiredError) as e:
                sess["code"] = ""
                await event.edit(
                    f"کد اشتباه یا منقضی شده، دوباره وارد کن:\n{e}",
                    buttons=code_keypad(""),
                )
                return
            except Exception as e:
                LOGIN_SESSIONS.pop(admin_id, None)
                try:
                    await sess["client"].disconnect()
                except Exception:
                    pass
                await event.edit(f"خطا: {e}", buttons=main_menu_buttons())
                return

            await finish_login(event, admin_id)
            return


# باید قبل از template_handler ثبت بشه تا اول این چک بشه
@bot.on(events.NewMessage(func=lambda e: e.text and not e.text.startswith("/")))
async def pending_input_handler(event):
    if not is_admin(event.sender_id):
        return
    admin_id = event.sender_id
    pending_key = PENDING.get(admin_id)
    if not pending_key:
        return  # چیزی منتظر نیست، بذار هندلرهای بعدی پردازش کنن

    value = event.raw_text.strip()

    if pending_key == "add_admin":
        if not value.isdigit():
            await send_or_edit(
                admin_id, event, "باید فقط آیدی عددی بفرستی.",
                buttons=[[Button.inline("انصراف", data="menu:admins", style="danger")]],
            )
            raise events.StopPropagation
        db.add_admin(int(value))
        PENDING.pop(admin_id, None)
        await send_or_edit(admin_id, event, f"ادمین {value} اضافه شد.", buttons=main_menu_buttons())
        PENDING_MSG.pop(admin_id, None)
        raise events.StopPropagation

    if pending_key == "add_dest_channel_target":
        TEMP_DEST_CHANNEL[admin_id] = value
        PENDING[admin_id] = "add_dest_channel_display"
        await send_or_edit(
            admin_id, event,
            "حالا متنی که به جای «ایدی کانال» توی پیام جایگزین بشه رو بفرست\n"
            "(اگه لازم نداری، فقط یه خط تیره - بفرست):",
            buttons=[[Button.inline("انصراف", data="menu:destchannels", style="danger")]],
        )
        raise events.StopPropagation

    if pending_key == "add_dest_channel_display":
        target = TEMP_DEST_CHANNEL.pop(admin_id, None)
        PENDING.pop(admin_id, None)
        if not target:
            await send_or_edit(admin_id, event, "چیزی برای افزودن پیدا نشد، دوباره از پنل شروع کن.", buttons=main_menu_buttons())
            PENDING_MSG.pop(admin_id, None)
            raise events.StopPropagation
        display = "" if value == "-" else value
        db.add_dest_channel(target, display)
        await send_or_edit(admin_id, event, f"کانال مقصد «{display or target}» اضافه شد.", buttons=dest_channels_menu_buttons())
        PENDING_MSG.pop(admin_id, None)
        raise events.StopPropagation

    if pending_key == "add_src_channel":
        PENDING.pop(admin_id, None)
        if not session_connected():
            await send_or_edit(admin_id, event, "اول باید سشن وصل باشه (از پنل «ورود با شماره» یا Session String بزن).", buttons=src_channels_menu_buttons())
            PENDING_MSG.pop(admin_id, None)
            raise events.StopPropagation
        joined = await join_from_identifier(value)
        if not joined:
            await send_or_edit(admin_id, event, "عضویت انجام نشد. لینک رو چک کن.", buttons=src_channels_menu_buttons())
            PENDING_MSG.pop(admin_id, None)
            raise events.StopPropagation
        try:
            target = value if not value.lstrip("-").isdigit() else int(value)
            entity = await user.get_entity(target)
            db.add_src_channel(entity.id, getattr(entity, "title", value))
            await send_or_edit(admin_id, event, f"کانال مبدا اضافه شد: {getattr(entity, 'title', value)}", buttons=src_channels_menu_buttons())
        except Exception as e:
            await send_or_edit(admin_id, event, f"خطا: {e}", buttons=src_channels_menu_buttons())
        PENDING_MSG.pop(admin_id, None)
        raise events.StopPropagation

    if pending_key == "login_phone":
        PENDING.pop(admin_id, None)
        await start_login_phone(event, value)
        raise events.StopPropagation

    if pending_key == "login_session_string":
        PENDING.pop(admin_id, None)
        try:
            await reconnect_user_client(value)
        except Exception as e:
            await send_or_edit(admin_id, event, f"استرینگ نامعتبر بود یا وصل نشد:\n{e}", buttons=main_menu_buttons())
            PENDING_MSG.pop(admin_id, None)
            raise events.StopPropagation
        db.set_setting("session_string", value)
        await send_or_edit(
            admin_id, event,
            f"وصل شد و ذخیره شد.\n\nSession String:\n`{value}`",
            buttons=main_menu_buttons(),
            parse_mode="markdown",
        )
        PENDING_MSG.pop(admin_id, None)
        raise events.StopPropagation

    if pending_key == "login_password":
        sess = LOGIN_SESSIONS.get(admin_id)
        try:
            await event.delete()
        except Exception:
            pass
        if not sess:
            PENDING.pop(admin_id, None)
            await send_or_edit(admin_id, event, "جلسه‌ی ورود پیدا نشد، دوباره از پنل شروع کن.", buttons=main_menu_buttons())
            PENDING_MSG.pop(admin_id, None)
            raise events.StopPropagation
        try:
            await sess["client"].sign_in(password=value)
        except PasswordHashInvalidError:
            await send_or_edit(
                admin_id, event, "رمز اشتباهه، دوباره بفرست:",
                buttons=[[Button.inline("انصراف", data="login:cancel", style="danger")]],
            )
            raise events.StopPropagation
        except Exception as e:
            PENDING.pop(admin_id, None)
            LOGIN_SESSIONS.pop(admin_id, None)
            try:
                await sess["client"].disconnect()
            except Exception:
                pass
            await send_or_edit(admin_id, event, f"خطا: {e}", buttons=main_menu_buttons())
            PENDING_MSG.pop(admin_id, None)
            raise events.StopPropagation

        PENDING.pop(admin_id, None)
        await finish_login(event, admin_id)
        raise events.StopPropagation

    # حالت عادی: یکی از تنظیمات ساده
    if pending_key in NUMERIC_SETTINGS:
        if not value.isdigit() or int(value) <= 0:
            label = SETTINGS_LABELS.get(pending_key, pending_key)
            await send_or_edit(
                admin_id, event,
                f"{label}\nفقط یه عدد صحیح مثبت قبول میشه (مثلا 30)، دوباره بفرست:",
                buttons=[[Button.inline("انصراف", data="menu:main", style="danger")]],
            )
            raise events.StopPropagation  # PENDING پاک نمیشه تا دوباره امتحان کنه

    db.set_setting(pending_key, value)
    PENDING.pop(admin_id, None)
    label = SETTINGS_LABELS.get(pending_key, pending_key)
    await send_or_edit(admin_id, event, f"{label} تنظیم شد روی:\n{value}", buttons=main_menu_buttons())
    PENDING_MSG.pop(admin_id, None)
    raise events.StopPropagation


# ---------------------------------------------------------------- core flow
async def get_link_from_bot1(file_path: str) -> str:
    if not session_connected():
        raise RuntimeError("سشن وصل نیست؛ از پنل روی «ورود با شماره» یا «Session String» بزن")

    bot1 = db.get_setting("bot1_username")
    if not bot1:
        raise RuntimeError("اول از پنل، ربات اول رو تنظیم کن")

    async with user.conversation(bot1, timeout=180) as conv:
        await call_with_flood_retry(lambda: conv.send_file(file_path), context="ارسال فایل به ربات اول")
        resp = await conv.get_response()
        return extract_link(resp.raw_text)


async def get_link_from_bot2(link: str) -> str:
    bot2 = db.get_setting("bot2_username")
    if not bot2:
        raise RuntimeError("اول از پنل، ربات دوم رو تنظیم کن")

    async with user.conversation(bot2, timeout=180) as conv:
        # هر بار قبل از /admin باید /start بزنیم، وگرنه پنل بالا نمیاد
        await call_with_flood_retry(lambda: conv.send_message("/start"), context="ارسال /start به ربات دوم")
        await conv.get_response()

        await call_with_flood_retry(lambda: conv.send_message("/admin"), context="ارسال /admin به ربات دوم")
        resp = await conv.get_response()

        # دکمه‌ی «آپلود فایل»: کلید چهارم کیبورد = ردیف دوم، کلید دوم (i, j از صفر شمرده میشه)
        await call_with_flood_retry(lambda: resp.click(i=1, j=1), context="کلیک آپلود فایل ربات دوم")
        resp = await conv.get_response()

        # دکمه‌ی «تکی»: ردیف اول، کلید اول (سمت چپ)
        await call_with_flood_retry(lambda: resp.click(i=0, j=0), context="کلیک تکی ربات دوم")
        resp = await conv.get_response()  # منتظر پرامپت لینک

        await call_with_flood_retry(lambda: conv.send_message(link), context="ارسال لینک به ربات دوم")
        resp = await conv.get_response()  # پیام حاوی لینک نهایی

        final_link = extract_link(resp.raw_text)

        # دکمه‌ی «بازگشت»: فقط یه دکمه‌ست، همون تک ردیف/تک کلید (i=0, j=0)
        try:
            await resp.click(i=0, j=0)
        except Exception as e:
            log.warning("back button click failed: %s", e)

        return final_link


async def process_file(event):
    status = await event.reply("در حال پردازش، صبر کن...")
    file_path = None
    try:
        file_path = await bot.download_media(event.message)
        async with session_lock:
            link1 = await get_link_from_bot1(file_path)
            final_link = await get_link_from_bot2(link1)

        db.set_last_link(event.sender_id, final_link)
        trigger_word = db.get_setting("trigger_word") or "مشاهده"
        await status.edit(
            f"لینک نهایی:\n{final_link}\n\n"
            f"حالا کپشن رو بفرست (باید کلمه‌ی «{trigger_word}» توش باشه تا پست بشه)."
        )
    except Exception as e:
        log.exception("process_file failed")
        await status.edit(f"خطا: {e}")
    finally:
        if file_path and os.path.exists(file_path):
            os.remove(file_path)


def build_channel_message(raw_text: str, link: str, channel_display: str, trigger_word: str) -> str:
    text = raw_text.replace("ایدی کانال", channel_display or "")
    text = text.replace(trigger_word, f'<a href="{link}">{trigger_word}</a>')
    return text


async def post_to_channel(channel: dict, raw_text: str, link: str):
    """پست کردن توی کانال مقصد با اکانت سشن (یوزربات)، نه ربات.
    ربات اصلی (BotFather) هیچوقت پیام رو نمیفرسته و لازم نیست عضو یا ادمین
    هیچ کانال یا ربات اول/دومی باشه؛ فقط پنل تنظیمات رو نشون میده."""
    if not session_connected():
        raise RuntimeError("سشن وصل نیست؛ از پنل روی «ورود با شماره» یا «Session String» بزن")
    trigger_word = db.get_setting("trigger_word") or "مشاهده"
    final_text = build_channel_message(raw_text, link, channel["display"], trigger_word)
    name = channel["display"] or channel["target"]
    await call_with_flood_retry(
        lambda: user.send_message(channel["target"], final_text, parse_mode="html", link_preview=False),
        context=f"ارسال به {name}",
    )


async def notify_admins(text: str):
    for admin_id in db.list_admins():
        try:
            await bot.send_message(admin_id, text)
        except Exception:
            pass


async def auto_post_after_watch(source_title: str, final_link: str, source_caption: str):
    """بعد از گرفتن لینک نهایی برای فایلی که مانیتور پیدا کرده، خودکار پست میکنه.
    قالب (کپشن) از همون پست کانال مبدا گرفته میشه؛ اگه پست مبدا کپشن نداشت،
    به «قالب جایگزین» (تنظیم‌شده از پنل) برمیگرده."""
    template = source_caption or db.get_setting("post_template")
    if not template:
        await notify_admins(
            f"فایل جدید از «{source_title}» گرفته شد ولی نه کپشنی داشت نه قالب جایگزین تنظیم شده.\n"
            f"لینک نهایی:\n{final_link}"
        )
        return

    channels = db.list_enabled_dest_channels()
    if not channels:
        await notify_admins(
            f"فایل جدید از «{source_title}» گرفته شد ولی کانال مقصد فعالی ثبت نشده.\nلینک نهایی:\n{final_link}"
        )
        return

    for channel in channels:
        try:
            await post_to_channel(channel, template, final_link)
        except Exception as e:
            log.warning("auto post failed for %s: %s", channel["target"], e)
            await notify_admins(
                f"ارسال خودکار به «{channel['display'] or channel['target']}» با خطا مواجه شد: {e}\n"
                f"لینک نهایی:\n{final_link}"
            )


async def poll_src_channels():
    global SESSION_ALERT_SENT
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    while True:
        interval_minutes = int(db.get_setting("watch_interval_minutes") or 60)
        interval = interval_minutes * 60
        max_per_day = int(db.get_setting("watch_max_per_day") or 5)
        try:
            healthy = await session_health_ok()
            if not healthy:
                if not SESSION_ALERT_SENT:
                    await notify_admins(
                        "⚠️ سشن (یوزربات) قطع شده یا دیگه معتبر نیست؛ ارسال خودکار متوقفه.\n"
                        "از پنل روی «ورود با شماره» یا «Session String» بزن و دوباره وصل کن."
                    )
                    SESSION_ALERT_SENT = True
            else:
                if SESSION_ALERT_SENT:
                    await notify_admins("✅ سشن دوباره وصل و معتبره؛ ارسال خودکار از سر گرفته شد.")
                SESSION_ALERT_SENT = False

            paused = db.get_setting("auto_paused") == "1"
            if healthy and not paused:
                elapsed = time.time() - db.last_download_time()
                if elapsed >= interval and db.downloads_in_last_24h() < max_per_day:
                    for ch in db.list_src_channels():
                        try:
                            target = ch["target"]
                            entity = await user.get_entity(int(target) if str(target).lstrip("-").isdigit() else target)
                            msgs = await user.get_messages(entity, limit=1)
                            if not msgs:
                                continue
                            latest = msgs[0]
                            if not (latest.document or latest.file):
                                continue
                            fuid = latest.file.id if latest.file else str(latest.id)
                            if db.already_downloaded(fuid):
                                continue

                            source_caption = latest.raw_text or ""

                            file_path = await user.download_media(latest, file=f"{DOWNLOAD_DIR}/")
                            db.mark_downloaded(fuid, target)

                            async with session_lock:
                                link1 = await get_link_from_bot1(file_path)
                                final_link = await get_link_from_bot2(link1)

                            if file_path and os.path.exists(file_path):
                                os.remove(file_path)

                            await auto_post_after_watch(ch["title"] or str(target), final_link, source_caption)
                            log.info("watcher processed file from %s", ch["title"] or target)

                            if db.downloads_in_last_24h() >= max_per_day:
                                break
                        except Exception as e:
                            log.warning("watch error for %s: %s", ch.get("target"), e)
        except Exception:
            log.exception("poll_src_channels loop error")

        await asyncio.sleep(min(interval, 60) if interval > 60 else interval)


# ---------------------------------------------------------------- file intake
@bot.on(events.NewMessage(func=lambda e: e.file is not None and not e.message.text))
async def file_handler(event):
    if not is_admin(event.sender_id):
        return
    await process_file(event)


# ---------------------------------------------------------------- caption -> channel post
@bot.on(events.NewMessage(func=lambda e: e.text and not e.text.startswith("/")))
async def template_handler(event):
    if not is_admin(event.sender_id):
        return
    trigger_word = db.get_setting("trigger_word") or "مشاهده"
    if trigger_word not in event.text:
        return

    link = db.get_last_link(event.sender_id)
    if not link:
        await event.reply("هنوز لینک نهایی برای این چت ثبت نشده؛ اول یه فایل بفرست.")
        return

    channels = db.list_enabled_dest_channels()
    if not channels:
        await event.reply(
            "کانال مقصد فعالی وجود نداره؛ از پنل روی «کانال‌های مقصد» بزن و یکی رو اضافه/فعال کن."
        )
        return

    ok, failed = [], []
    for channel in channels:
        name = channel["display"] or channel["target"]
        try:
            await post_to_channel(channel, event.text, link)
            ok.append(name)
        except (RPCError, RuntimeError) as e:
            failed.append(f"{name}: {e}")

    lines = []
    if ok:
        lines.append("پست شد توی: " + "، ".join(ok))
    if failed:
        lines.append("ارسال با خطا مواجه شد:\n" + "\n".join(failed))
    await event.reply("\n".join(lines) if lines else "هیچ کانالی برای ارسال پیدا نشد.")


# ---------------------------------------------------------------- run
async def main():
    await bot.start(bot_token=BOT_TOKEN)

    stored_session = db.get_setting("session_string") or ENV_SESSION_STRING
    if stored_session:
        try:
            await reconnect_user_client(stored_session)
            log.info("session client connected")
        except Exception as e:
            log.warning("could not connect stored session: %s", e)
    else:
        log.warning("سشنی ثبت نشده - از /panel روی «ورود با شماره» یا «Session String» بزن")

    asyncio.create_task(poll_src_channels())

    log.info("bot started")
    await bot.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
