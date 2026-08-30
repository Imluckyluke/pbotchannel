import asyncio
import os
import re
import logging

from dotenv import load_dotenv
from telethon import TelegramClient, events, Button
from telethon.sessions import StringSession
from telethon.errors import (
    RPCError,
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    PasswordHashInvalidError,
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

db.init_db(bootstrap_admin_ids=ADMIN_IDS)

bot = TelegramClient(StringSession(), API_ID, API_HASH)
user = TelegramClient(StringSession(), API_ID, API_HASH)  # تا لاگین نشه وصل نیست

session_lock = asyncio.Lock()

PENDING = {}          # user_id -> کلید تنظیمی که منتظر مقدار متنیشیم
LOGIN_SESSIONS = {}    # user_id -> {client, phone, phone_code_hash, code}

SETTINGS_LABELS = {
    "bot_x_username": "ربات X",
    "backup_bot_username": "ربات بکاپ",
    "channel_target": "چنل مقصد (پست)",
    "channel_display": "نمایش آیدی چنل",
}


# ---------------------------------------------------------------- helpers
def is_admin(user_id: int) -> bool:
    return db.is_admin(user_id)


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


# ---------------------------------------------------------------- panel UI
def main_menu_buttons():
    rows = []
    for key, label in SETTINGS_LABELS.items():
        rows.append([Button.inline(label, data=f"set:{key}")])
    status = "متصل ✅" if session_connected() else "قطع ❌"
    rows.append([Button.inline(f"🔑 ورود با شماره (وضعیت: {status})", data="login:start")])
    rows.append([Button.inline("📋 ورود مستقیم با Session String", data="login:string")])
    rows.append([Button.inline("مدیریت ادمین‌ها", data="menu:admins")])
    rows.append([Button.inline("نمایش کامل تنظیمات", data="menu:show")])
    rows.append([Button.inline("بستن", data="menu:close")])
    return rows


def admins_menu_buttons():
    rows = [[Button.inline(f"❌ حذف {a}", data=f"admin:rm:{a}")] for a in db.list_admins()]
    rows.append([Button.inline("➕ افزودن ادمین", data="admin:add")])
    rows.append([Button.inline("بازگشت", data="menu:main")])
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
        Button.inline("⌫", data="login:back"),
        Button.inline("✅ تایید", data="login:submit"),
    ])
    rows.append([Button.inline("❌ انصراف", data="login:cancel")])
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
        await event.respond(f"خطا در ارسال کد: {e}", buttons=main_menu_buttons())
        return

    LOGIN_SESSIONS[admin_id] = {
        "client": temp_client,
        "phone": phone,
        "phone_code_hash": sent.phone_code_hash,
        "code": "",
    }
    await event.respond(
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
    if isinstance(edit_target, events.CallbackQuery.Event):
        await edit_target.edit(text, buttons=main_menu_buttons(), parse_mode="markdown")
    else:
        await edit_target.respond(text, buttons=main_menu_buttons(), parse_mode="markdown")


@bot.on(events.CallbackQuery())
async def callback_handler(event):
    if not is_admin(event.sender_id):
        await event.answer("دسترسی نداری", alert=True)
        return

    data = event.data.decode()
    admin_id = event.sender_id

    if data == "menu:main":
        PENDING.pop(admin_id, None)
        await event.edit("پنل تنظیمات:", buttons=main_menu_buttons())
        return

    if data == "menu:close":
        PENDING.pop(admin_id, None)
        await event.edit("بسته شد. برای باز کردن دوباره /panel رو بزن.")
        return

    if data == "menu:show":
        cfg = db.get_all_settings()
        lines = []
        for k, v in cfg.items():
            if k == "session_string":
                continue
            lines.append(f"{SETTINGS_LABELS.get(k, k)}: {v or '—'}")
        lines.append(f"وضعیت سشن: {'متصل ✅' if session_connected() else 'قطع ❌'}")
        lines.append(f"ادمین‌ها: {db.list_admins()}")
        await event.edit("\n".join(lines), buttons=[[Button.inline("بازگشت", data="menu:main")]])
        return

    if data == "menu:admins":
        PENDING.pop(admin_id, None)
        await event.edit("مدیریت ادمین‌ها:", buttons=admins_menu_buttons())
        return

    if data.startswith("admin:rm:"):
        uid = int(data.split(":")[2])
        db.remove_admin(uid)
        await event.edit(f"ادمین {uid} حذف شد.\n\nمدیریت ادمین‌ها:", buttons=admins_menu_buttons())
        return

    if data == "admin:add":
        PENDING[admin_id] = "add_admin"
        await event.edit(
            "آیدی عددی ادمین جدید رو بفرست:",
            buttons=[[Button.inline("انصراف", data="menu:admins")]],
        )
        return

    if data.startswith("set:"):
        key = data.split(":", 1)[1]
        PENDING[admin_id] = key
        current = db.get_setting(key)
        label = SETTINGS_LABELS.get(key, key)
        await event.edit(
            f"{label}\nمقدار فعلی: {current or '—'}\n\nمقدار جدید رو بفرست:",
            buttons=[[Button.inline("انصراف", data="menu:main")]],
        )
        return

    if data.startswith("login:"):
        action = data.split(":", 1)[1]

        if action == "start":
            LOGIN_SESSIONS.pop(admin_id, None)
            PENDING[admin_id] = "login_phone"
            await event.edit(
                "شماره تلفن اکانت سشن رو با فرمت بین‌المللی بفرست (مثلا +989123456789):",
                buttons=[[Button.inline("انصراف", data="menu:main")]],
            )
            return

        if action == "string":
            LOGIN_SESSIONS.pop(admin_id, None)
            PENDING[admin_id] = "login_session_string"
            await event.edit(
                "استرینگ سشن (Session String) رو بفرست:",
                buttons=[[Button.inline("انصراف", data="menu:main")]],
            )
            return

        if action == "cancel":
            PENDING.pop(admin_id, None)
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
                    buttons=[[Button.inline("انصراف", data="login:cancel")]],
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
            await event.reply("باید فقط آیدی عددی بفرستی.")
            raise events.StopPropagation
        db.add_admin(int(value))
        PENDING.pop(admin_id, None)
        await event.reply(f"ادمین {value} اضافه شد.", buttons=main_menu_buttons())
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
            await event.reply(
                f"استرینگ نامعتبر بود یا وصل نشد:\n{e}", buttons=main_menu_buttons()
            )
            raise events.StopPropagation
        db.set_setting("session_string", value)
        await event.reply(
            f"وصل شد و ذخیره شد.\n\nSession String:\n`{value}`",
            buttons=main_menu_buttons(),
            parse_mode="markdown",
        )
        raise events.StopPropagation

    if pending_key == "login_password":
        sess = LOGIN_SESSIONS.get(admin_id)
        try:
            await event.delete()
        except Exception:
            pass
        if not sess:
            PENDING.pop(admin_id, None)
            await event.respond("جلسه‌ی ورود پیدا نشد، دوباره از پنل شروع کن.", buttons=main_menu_buttons())
            raise events.StopPropagation
        try:
            await sess["client"].sign_in(password=value)
        except PasswordHashInvalidError:
            await event.respond("رمز اشتباهه، دوباره بفرست:", buttons=[[Button.inline("انصراف", data="login:cancel")]])
            raise events.StopPropagation
        except Exception as e:
            PENDING.pop(admin_id, None)
            LOGIN_SESSIONS.pop(admin_id, None)
            try:
                await sess["client"].disconnect()
            except Exception:
                pass
            await event.respond(f"خطا: {e}", buttons=main_menu_buttons())
            raise events.StopPropagation

        PENDING.pop(admin_id, None)
        await finish_login(event, admin_id)
        raise events.StopPropagation

    # حالت عادی: یکی از تنظیمات ساده
    db.set_setting(pending_key, value)
    PENDING.pop(admin_id, None)
    label = SETTINGS_LABELS.get(pending_key, pending_key)
    await event.reply(f"{label} تنظیم شد روی:\n{value}", buttons=main_menu_buttons())
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
        await status.edit(f"لینک بکاپ:\n{backup_link}")
    except Exception as e:
        log.exception("process_file failed")
        await status.edit(f"خطا: {e}")
    finally:
        if file_path and os.path.exists(file_path):
            os.remove(file_path)


def build_channel_message(raw_text: str, link: str, channel_display: str) -> str:
    text = raw_text.replace("ایدی چنل", channel_display or "")
    text = text.replace("مشاهده", f'<a href="{link}">مشاهده</a>')
    return text


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
    if "مشاهده" not in event.text:
        return

    link = db.get_last_link(event.sender_id)
    if not link:
        await event.reply("هنوز لینک بکاپی برای این چت ثبت نشده؛ اول یه فایل بفرست.")
        return

    channel_target = db.get_setting("channel_target")
    channel_display = db.get_setting("channel_display")
    if not channel_target:
        await event.reply("اول از پنل، چنل مقصد رو تنظیم کن.")
        return

    final_text = build_channel_message(event.text, link, channel_display)

    try:
        await bot.send_message(channel_target, final_text, parse_mode="html", link_preview=False)
        await event.reply("پست شد توی چنل.")
    except RPCError as e:
        await event.reply(f"ارسال به چنل با خطا مواجه شد: {e}")


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

    log.info("bot started")
    await bot.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
