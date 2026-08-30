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
log = logging.getLogger("session-backup-bot")

LINK_RE = re.compile(r"https?://\S+")
INVITE_LINK_RE = re.compile(r"(?:https?://)?t\.me/(?:joinchat/|\+)?([A-Za-z0-9_]+)")
DOWNLOAD_DIR = os.path.join(os.path.dirname(__file__), "downloads")

db.init_db(bootstrap_admin_ids=ADMIN_IDS)

bot = TelegramClient(StringSession(), API_ID, API_HASH)
user = TelegramClient(StringSession(), API_ID, API_HASH)  # تا لاگین نشه وصل نیست

session_lock = asyncio.Lock()

PENDING = {}          # user_id -> کلید تنظیمی که منتظر مقدار متنیشیم
LOGIN_SESSIONS = {}    # user_id -> {client, phone, phone_code_hash, code}

SETTINGS_LABELS = {
    "bot_x_username": "ربات X",
    "backup_bot_username": "ربات بکاپ",
    "trigger_word": "کلمه‌ی تبدیل به لینک",
    "watch_interval_seconds": "فاصله زمانی مانیتور (ثانیه)",
    "watch_max_per_day": "سقف روزانه مانیتور (فایل)",
    "post_template": "قالب پشتیبان (وقتی پست مبدا کپشن نداره)",
}

PENDING_TEMPLATE = {}   # user_id -> {"text": ..., "link": ...} تا وقتی کانال انتخاب بشه
TEMP_CHANNEL = {}       # user_id -> target موقت هنگام افزودن کانال جدید
PENDING_MSG = {}        # user_id -> (chat_id, message_id) پیام پنلی که باید ادیت بشه


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
    # همیشه لینک واقعی (بکاپ) ته پیامه؛ اگه لینک دیگه‌ای هم قبلش باشه
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


async def join_from_identifier(identifier: str) -> bool:
    """identifier: لینک t.me، یوزرنیم @، یا آیدی عددی چنلی که سشن از قبل عضوشه."""
    m = INVITE_LINK_RE.search(identifier)
    try:
        if m and ("/+" in identifier or "joinchat" in identifier):
            await user(ImportChatInviteRequest(m.group(1)))
        elif m:
            await user(JoinChannelRequest(m.group(1)))
        elif identifier.startswith("@"):
            await user(JoinChannelRequest(identifier))
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
def main_menu_buttons():
    rows = []
    for key, label in SETTINGS_LABELS.items():
        current = db.get_setting(key)
        val = current if current else "تنظیم‌نشده"
        style = "success" if current else "danger"
        rows.append([Button.inline(f"{label}: {val}", data=f"set:{key}", style=style)])
    connected = session_connected()
    status = "متصل" if connected else "قطع"
    rows.append([Button.inline(f"ورود با شماره ({status})", data="login:start", style="success" if connected else "danger")])
    rows.append([Button.inline("ورود مستقیم با Session String", data="login:string", style="primary")])
    rows.append([Button.inline(f"مدیریت کانال‌ها ({len(db.list_channels())})", data="menu:channels", style="primary")])
    rows.append([Button.inline(f"📡 چنل‌های مانیتور ({len(db.list_watched_channels())})", data="menu:watchchannels", style="primary")])
    rows.append([Button.inline(f"مدیریت ادمین‌ها ({len(db.list_admins())})", data="menu:admins", style="primary")])
    rows.append([Button.inline("نمایش کامل تنظیمات", data="menu:show")])
    rows.append([Button.inline("بستن", data="menu:close", style="danger")])
    return rows


def admins_menu_buttons():
    rows = [[Button.inline(f"حذف {a}", data=f"admin:rm:{a}", style="danger")] for a in db.list_admins()]
    rows.append([Button.inline("افزودن ادمین", data="admin:add", style="success")])
    rows.append([Button.inline("بازگشت", data="menu:main")])
    return rows


def channels_menu_buttons():
    rows = [
        [Button.inline(f"حذف {c['display'] or c['target']}", data=f"channel:rm:{c['id']}", style="danger")]
        for c in db.list_channels()
    ]
    rows.append([Button.inline("افزودن کانال", data="channel:add", style="success")])
    rows.append([Button.inline("بازگشت", data="menu:main")])
    return rows


def watch_channels_menu_buttons():
    rows = [
        [Button.inline(f"حذف {c['title'] or c['target']}", data=f"watchch:rm:{c['id']}", style="danger")]
        for c in db.list_watched_channels()
    ]
    rows.append([Button.inline("افزودن چنل مانیتور", data="watchch:add", style="success")])
    rows.append([Button.inline("بازگشت", data="menu:main")])
    return rows


def channel_pick_buttons():
    return [
        [Button.inline(c["display"] or c["target"], data=f"post:{c['id']}", style="primary")]
        for c in db.list_channels()
    ]


def code_keypad(code_so_far: str):
    rows, row = [], []
    for d in ["1", "2", "3", "4", "5", "6", "7", "8", "9"]:
        row.append(Button.inline(d, data=f"login:digit:{d}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    rows.append([
        Button.inline("0", data="login:digit:0"),
        Button.inline("پاک کردن", data="login:back", style="danger"),
        Button.inline("تایید", data="login:submit", style="success"),
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
        "آماده‌ام. یه فایل بفرست تا لینک بکاپ بگیرم، یا از پنل زیر همه چیزو تنظیم کن:",
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
        lines.append(f"ادمین‌ها: {db.list_admins()}")
        chans = db.list_channels()
        if chans:
            lines.append("کانال‌ها: " + ", ".join(c["display"] or c["target"] for c in chans))
        else:
            lines.append("کانال‌ها: —")
        wchans = db.list_watched_channels()
        if wchans:
            lines.append("چنل‌های مانیتور: " + ", ".join(c["title"] or c["target"] for c in wchans))
        else:
            lines.append("چنل‌های مانیتور: —")
        await event.edit("\n".join(lines), buttons=[[Button.inline("بازگشت", data="menu:main")]])
        return

    if data == "menu:channels":
        PENDING.pop(admin_id, None)
        PENDING_MSG.pop(admin_id, None)
        await event.edit("مدیریت کانال‌ها:", buttons=channels_menu_buttons())
        return

    if data == "channel:add":
        PENDING[admin_id] = "add_channel_target"
        PENDING_MSG[admin_id] = (event.chat_id, event.message_id)
        await event.edit(
            "آیدی عددی یا یوزرنیم کانال (مثلا @mychannel) رو بفرست:",
            buttons=[[Button.inline("انصراف", data="menu:channels", style="danger")]],
        )
        return

    if data.startswith("channel:rm:"):
        cid = int(data.split(":")[2])
        db.remove_channel(cid)
        await event.edit("کانال حذف شد.\n\nمدیریت کانال‌ها:", buttons=channels_menu_buttons())
        return

    if data.startswith("post:"):
        cid = int(data.split(":", 1)[1])
        pending = PENDING_TEMPLATE.pop(admin_id, None)
        if not pending:
            await event.answer("این درخواست منقضی شده، دوباره پیام رو بفرست.", alert=True)
            return
        channel = db.get_channel(cid)
        if not channel:
            await event.answer("این کانال دیگه وجود نداره.", alert=True)
            return
        try:
            await post_to_channel(channel, pending["text"], pending["link"])
            await event.edit(f"پست شد توی «{channel['display'] or channel['target']}».")
        except Exception as e:
            await event.edit(f"ارسال به کانال با خطا مواجه شد: {e}")
        return

    if data == "menu:watchchannels":
        PENDING.pop(admin_id, None)
        PENDING_MSG.pop(admin_id, None)
        await event.edit("چنل‌های مانیتور (که ازشون فایل جدید گرفته میشه):", buttons=watch_channels_menu_buttons())
        return

    if data == "watchch:add":
        PENDING[admin_id] = "add_watch_channel"
        PENDING_MSG[admin_id] = (event.chat_id, event.message_id)
        await event.edit(
            "لینک دعوت، یوزرنیم (@channel) یا آیدی عددی چنل مبدا رو بفرست:",
            buttons=[[Button.inline("انصراف", data="menu:watchchannels", style="danger")]],
        )
        return

    if data.startswith("watchch:rm:"):
        cid = int(data.split(":")[2])
        db.remove_watched_channel(cid)
        await event.edit("چنل مانیتور حذف شد.\n\nچنل‌های مانیتور:", buttons=watch_channels_menu_buttons())
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

    if pending_key == "add_channel_target":
        TEMP_CHANNEL[admin_id] = value
        PENDING[admin_id] = "add_channel_display"
        await send_or_edit(
            admin_id, event,
            "حالا متنی که به جای «ایدی چنل» توی پیام جایگزین بشه رو بفرست\n"
            "(اگه لازم نداری، فقط یه خط تیره - بفرست):",
            buttons=[[Button.inline("انصراف", data="menu:channels", style="danger")]],
        )
        raise events.StopPropagation

    if pending_key == "add_channel_display":
        target = TEMP_CHANNEL.pop(admin_id, None)
        PENDING.pop(admin_id, None)
        if not target:
            await send_or_edit(admin_id, event, "چیزی برای افزودن پیدا نشد، دوباره از پنل شروع کن.", buttons=main_menu_buttons())
            PENDING_MSG.pop(admin_id, None)
            raise events.StopPropagation
        display = "" if value == "-" else value
        db.add_channel(target, display)
        await send_or_edit(admin_id, event, f"کانال «{display or target}» اضافه شد.", buttons=channels_menu_buttons())
        PENDING_MSG.pop(admin_id, None)
        raise events.StopPropagation

    if pending_key == "add_watch_channel":
        PENDING.pop(admin_id, None)
        if not session_connected():
            await send_or_edit(admin_id, event, "اول باید سشن وصل باشه (از پنل «ورود با شماره» یا Session String بزن).", buttons=watch_channels_menu_buttons())
            PENDING_MSG.pop(admin_id, None)
            raise events.StopPropagation
        joined = await join_from_identifier(value)
        if not joined:
            await send_or_edit(admin_id, event, "عضویت انجام نشد. لینک رو چک کن.", buttons=watch_channels_menu_buttons())
            PENDING_MSG.pop(admin_id, None)
            raise events.StopPropagation
        try:
            target = value if not value.lstrip("-").isdigit() else int(value)
            entity = await user.get_entity(target)
            db.add_watched_channel(entity.id, getattr(entity, "title", value))
            await send_or_edit(admin_id, event, f"چنل مانیتور اضافه شد: {getattr(entity, 'title', value)}", buttons=watch_channels_menu_buttons())
        except Exception as e:
            await send_or_edit(admin_id, event, f"خطا: {e}", buttons=watch_channels_menu_buttons())
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
    db.set_setting(pending_key, value)
    PENDING.pop(admin_id, None)
    label = SETTINGS_LABELS.get(pending_key, pending_key)
    await send_or_edit(admin_id, event, f"{label} تنظیم شد روی:\n{value}", buttons=main_menu_buttons())
    PENDING_MSG.pop(admin_id, None)
    raise events.StopPropagation


# ---------------------------------------------------------------- core flow
async def get_link_from_bot_x(file_path: str) -> str:
    if not session_connected():
        raise RuntimeError("سشن وصل نیست؛ از پنل روی «ورود سشن جدید» بزن")

    bot_x = db.get_setting("bot_x_username")
    if not bot_x:
        raise RuntimeError("اول از پنل، ربات X رو تنظیم کن")

    async with user.conversation(bot_x, timeout=180) as conv:
        await conv.send_file(file_path)
        resp = await conv.get_response()
        return extract_link(resp.raw_text)


async def get_backup_link(link: str) -> str:
    backup_bot = db.get_setting("backup_bot_username")
    if not backup_bot:
        raise RuntimeError("اول از پنل، ربات بکاپ رو تنظیم کن")

    async with user.conversation(backup_bot, timeout=180) as conv:
        # هر بار قبل از /admin باید /start بزنیم، وگرنه پنل بالا نمیاد
        await conv.send_message("/start")
        await conv.get_response()

        await conv.send_message("/admin")
        resp = await conv.get_response()

        # دکمه‌ی «آپلود فایل»: کلید چهارم کیبورد = ردیف دوم، کلید دوم (i, j از صفر شمرده میشه)
        await resp.click(i=1, j=1)
        resp = await conv.get_response()

        # دکمه‌ی «تکی»: ردیف اول، کلید اول (سمت چپ)
        await resp.click(i=0, j=0)
        resp = await conv.get_response()  # منتظر پرامپت لینک

        await conv.send_message(link)
        resp = await conv.get_response()  # پیام حاوی لینک بکاپ

        backup_link = extract_link(resp.raw_text)

        # دکمه‌ی «بازگشت»: فقط یه دکمه‌ست، همون تک ردیف/تک کلید (i=0, j=0)
        try:
            await resp.click(i=0, j=0)
        except Exception as e:
            log.warning("back button click failed: %s", e)

        return backup_link


async def process_file(event):
    status = await event.reply("در حال پردازش، صبر کن...")
    file_path = None
    try:
        file_path = await bot.download_media(event.message)
        async with session_lock:
            link1 = await get_link_from_bot_x(file_path)
            backup_link = await get_backup_link(link1)

        db.set_last_link(event.sender_id, backup_link)
        trigger_word = db.get_setting("trigger_word") or "مشاهده"
        await status.edit(
            f"لینک بکاپ:\n{backup_link}\n\n"
            f"حالا کپشن رو بفرست (باید کلمه‌ی «{trigger_word}» توش باشه تا پست بشه)."
        )
    except Exception as e:
        log.exception("process_file failed")
        await status.edit(f"خطا: {e}")
    finally:
        if file_path and os.path.exists(file_path):
            os.remove(file_path)


def build_channel_message(raw_text: str, link: str, channel_display: str, trigger_word: str) -> str:
    text = raw_text.replace("ایدی چنل", channel_display or "")
    text = text.replace(trigger_word, f'<a href="{link}">{trigger_word}</a>')
    return text


async def post_to_channel(channel: dict, raw_text: str, link: str):
    """پست کردن توی کانال با اکانت سشن (یوزربات)، نه ربات."""
    if not session_connected():
        raise RuntimeError("سشن وصل نیست؛ از پنل روی «ورود با شماره» یا «Session String» بزن")
    trigger_word = db.get_setting("trigger_word") or "مشاهده"
    final_text = build_channel_message(raw_text, link, channel["display"], trigger_word)
    await user.send_message(channel["target"], final_text, parse_mode="html", link_preview=False)


async def notify_admins(text: str):
    for admin_id in db.list_admins():
        try:
            await bot.send_message(admin_id, text)
        except Exception:
            pass


async def auto_post_after_watch(source_title: str, backup_link: str, source_caption: str):
    """بعد از گرفتن لینک بکاپ برای فایلی که مانیتور پیدا کرده، خودکار پست میکنه.
    قالب (کپشن) از همون پست مبدا گرفته میشه؛ اگه پست مبدا کپشن نداشت،
    به «قالب پست خودکار» (تنظیم‌شده از پنل) به عنوان جایگزین برمیگرده."""
    template = source_caption or db.get_setting("post_template")
    if not template:
        await notify_admins(
            f"فایل جدید از «{source_title}» گرفته شد ولی نه کپشنی داشت نه قالب پست خودکار تنظیم شده.\n"
            f"لینک بکاپ:\n{backup_link}"
        )
        return

    channels = db.list_channels()
    if not channels:
        await notify_admins(
            f"فایل جدید از «{source_title}» گرفته شد ولی کانال مقصدی ثبت نشده.\nلینک بکاپ:\n{backup_link}"
        )
        return

    for channel in channels:
        try:
            await post_to_channel(channel, template, backup_link)
        except Exception as e:
            log.warning("auto post failed for %s: %s", channel["target"], e)
            await notify_admins(
                f"ارسال خودکار به «{channel['display'] or channel['target']}» با خطا مواجه شد: {e}\n"
                f"لینک بکاپ:\n{backup_link}"
            )


async def poll_watched_channels():
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    while True:
        interval = int(db.get_setting("watch_interval_seconds") or 3600)
        max_per_day = int(db.get_setting("watch_max_per_day") or 5)
        try:
            if session_connected():
                elapsed = time.time() - db.last_download_time()
                if elapsed >= interval and db.downloads_in_last_24h() < max_per_day:
                    for ch in db.list_watched_channels():
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
                                link1 = await get_link_from_bot_x(file_path)
                                backup_link = await get_backup_link(link1)

                            if file_path and os.path.exists(file_path):
                                os.remove(file_path)

                            await auto_post_after_watch(ch["title"] or str(target), backup_link, source_caption)
                            log.info("watcher processed file from %s", ch["title"] or target)

                            if db.downloads_in_last_24h() >= max_per_day:
                                break
                        except Exception as e:
                            log.warning("watch error for %s: %s", ch.get("target"), e)
        except Exception:
            log.exception("poll_watched_channels loop error")

        await asyncio.sleep(min(interval, 60) if interval > 60 else interval)


# ---------------------------------------------------------------- file intake
@bot.on(events.NewMessage(func=lambda e: e.file is not None and not e.message.text))
async def file_handler(event):
    if not is_admin(event.sender_id):
        return
    await process_file(event)


# ---------------------------------------------------------------- template -> channel post
@bot.on(events.NewMessage(func=lambda e: e.text and not e.text.startswith("/")))
async def template_handler(event):
    if not is_admin(event.sender_id):
        return
    trigger_word = db.get_setting("trigger_word") or "مشاهده"
    if trigger_word not in event.text:
        return

    link = db.get_last_link(event.sender_id)
    if not link:
        await event.reply("هنوز لینک بکاپی برای این چت ثبت نشده؛ اول یه فایل بفرست.")
        return

    channels = db.list_channels()
    if not channels:
        await event.reply("هنوز کانالی اضافه نشده؛ از پنل روی «مدیریت کانال‌ها» بزن.")
        return

    if len(channels) == 1:
        try:
            await post_to_channel(channels[0], event.text, link)
            await event.reply("پست شد توی کانال.")
        except (RPCError, RuntimeError) as e:
            await event.reply(f"ارسال به کانال با خطا مواجه شد: {e}")
        return

    PENDING_TEMPLATE[event.sender_id] = {"text": event.text, "link": link}
    await event.reply("به کدوم کانال ارسال بشه؟", buttons=channel_pick_buttons())


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
        log.warning("سشنی ثبت نشده - از /panel روی «ورود سشن جدید» بزن")

    asyncio.create_task(poll_watched_channels())

    log.info("bot started")
    await bot.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
