import asyncio
import os
import re
import json
import time
import uuid
import logging

from dotenv import load_dotenv
from telethon import TelegramClient, events, Button
from telethon.sessions import StringSession
from telethon.tl.functions.channels import JoinChannelRequest, LeaveChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest, CheckChatInviteRequest
from telethon.tl.types import (
    MessageEntityTextUrl, MessageEntityUrl,
    Channel, Chat, ChatInviteAlready,
)
from telethon.errors import (
    RPCError,
    FloodWaitError,
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    PasswordHashInvalidError,
    UserAlreadyParticipantError,
    InviteHashExpiredError,
    InviteRequestSentError,
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
START_LINK_RE = re.compile(r"t\.me/([A-Za-z0-9_]+)\?start=([A-Za-z0-9_\-]+)", re.IGNORECASE)
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
    ("trigger_word", "🔗 کلمه(های) تبدیل به لینک (با کاما جدا کن)"),
]

# تنظیمات مربوط به بررسی خودکار کانال‌های مبدا
WATCH_SETTINGS = [
    ("watch_interval_minutes", "⏱ فاصله زمانی بین هر پست (دقیقه)"),
    ("watch_max_per_day", "🔢 تعداد پست روزانه"),
    ("post_template", "📝 قالب جایگزین (بدون کپشن مبدا)"),
    ("gate_leave_minutes", "🚪 لفت از کانال‌های گیت بعد از (دقیقه، ۰=هیچوقت)"),
]

SETTINGS_LABELS = {key: label for key, label in CORE_SETTINGS + WATCH_SETTINGS}
NUMERIC_SETTINGS = {"watch_interval_minutes", "watch_max_per_day", "gate_leave_minutes"}

TEMP_DEST_CHANNEL = {}    # user_id -> target موقت هنگام افزودن کانال مقصد جدید
PENDING_MSG = {}          # user_id -> (chat_id, message_id) پیام پنلی که باید ادیت بشه

SESSION_ALERT_SENT = False   # تا وقتی سشن خرابه دوباره و دوباره به ادمین‌ها پیام نده

# --------- حالت‌های موقتِ رفع‌مشکل دستی (فقط تو حافظه؛ با ری‌استارت پاک میشن) ---------
RETRY_CONTEXTS = {}       # token -> {"title","target","source_link","source_caption","dedup_key"}
MANUAL_POST_CONTEXTS = {}  # (admin_id, prompt_message_id) -> {"final_link","source_title"}


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


def extract_source_link(message) -> str:
    """لینکِ زیر پست کانال مبدا رو در میاره؛ چه هایپرلینک روی یه کلمه باشه
    (مثل «مشاهده» که خودمون هم همینجوری پست می‌کنیم) چه لینک خام توی متن."""
    if not message:
        return None
    if message.entities:
        for e in message.entities:
            if isinstance(e, MessageEntityTextUrl):
                return e.url
        text = message.raw_text or ""
        for e in message.entities:
            if isinstance(e, MessageEntityUrl):
                return text[e.offset:e.offset + e.length]
    if message.raw_text:
        m = LINK_RE.search(message.raw_text)
        if m:
            return m.group(0)
    return None


def parse_start_link(link: str):
    """t.me/<username>?start=<param> رو پارس میکنه؛ اگه فرمتش این نبود None برمیگردونه."""
    m = START_LINK_RE.search(link or "")
    if not m:
        return None
    return m.group(1), m.group(2)


def find_start_link_in_message(message) -> str:
    """توی یه پیامِ جوابِ ربات (متن، هایپرلینک، یا دکمه‌های url‌دار) دنبال یه
    دیپ‌لینک جدید (t.me/username?start=...) میگرده؛ برای حالتی که ربات اول
    به‌جای گیت عضویت یا فایل، یه لینک دیگه (همون ربات یا یه ربات دوم) برمیگردونه."""
    candidates = []
    if message.buttons:
        for row in message.buttons:
            for btn in row:
                url = getattr(btn, "url", None)
                if url:
                    candidates.append(url)
    if message.entities:
        for e in message.entities:
            if isinstance(e, MessageEntityTextUrl):
                candidates.append(e.url)
        text = message.raw_text or ""
        for e in message.entities:
            if isinstance(e, MessageEntityUrl):
                candidates.append(text[e.offset:e.offset + e.length])
    if message.raw_text:
        candidates.extend(LINK_RE.findall(message.raw_text))
    for c in candidates:
        if parse_start_link(c):
            return c
    return None


async def fetch_file_from_start_link(link: str, max_hops: int = 6, max_inner_retries: int = 10):
    """دیپ‌لینکِ زیر پست کانال مبدا رو باز میکنه (وارد ربات مقصد میشه با /start)
    و پیامِ حاوی فایل رو برمیگردونه (نه مسیر یه فایل دانلودشده — فایل اصلاً
    روی سرور ما دانلود نمیشه؛ بعداً موقع فرستادن به ربات اول، Telethon خودش
    فایل رو سمت سرورهای تلگرام کپی میکنه، که هم سریع‌تره هم فضای دیسک سرور
    رو اشغال نمی‌کنه).
    دو حالت رو پشتیبانی میکنه:
    - ربات همون اول گیت عضویت (join gate) نشون میده: دکمه‌های عضویت رو پیدا و
      عضو میشه، بعد دکمه‌ی تایید رو میزنه (یا دوباره /start میفرسته)، تا فایل بیاد.
    - ربات به‌جای گیت یا فایل، یه دیپ‌لینک دیگه برمیگردونه (همون ربات با
      پارامتر متفاوت، یا یه ربات دومِ کاملاً جدا) — این حالت رو هم دنبال
      میکنه (max_hops بار) تا برسه به جایی که واقعاً فایل یا گیت عضویت باشه."""
    current_link = link
    visited = set()
    gate_leave_minutes = int(db.get_setting("gate_leave_minutes") or 30) or None

    for hop in range(max_hops):
        if current_link in visited:
            raise RuntimeError(f"لینک تکراری دیده شد (احتمالاً حلقه‌ست): {current_link}")
        visited.add(current_link)

        parsed = parse_start_link(current_link)
        if not parsed:
            raise RuntimeError(f"لینک دیپ‌لینک قابل تشخیص نبود (فرمت t.me/username?start=... نیست): {current_link}")
        bot_username, start_param = parsed
        log.info("deeplink: مرحله %s/%s - باز کردن %s (start=%s)", hop + 1, max_hops, bot_username, start_param)

        async with user.conversation(bot_username, timeout=60) as conv:
            await call_with_flood_retry(
                lambda: conv.send_message(f"/start {start_param}"),
                context=f"باز کردن دیپ‌لینک {bot_username}",
            )
            resp = await conv.get_response()
            log.info(
                "deeplink: %s -> اولین پاسخ رسید (فایل داره: %s، تعداد ردیف دکمه: %s)",
                bot_username, bool(resp.document or resp.photo or resp.file),
                len(resp.buttons) if resp.buttons else 0,
            )
            if not resp.buttons and not (resp.document or resp.photo or resp.file):
                log.info("deeplink: %s -> متن این پیام: %r", bot_username, resp.raw_text)

            advanced_to_next_hop = False
            unresolved_join_links = set()

            for attempt in range(max_inner_retries):
                if resp.document or resp.photo or resp.file:
                    log.info("deeplink: %s -> فایل رسید (بدون دانلود، مستقیم فوروارد میشه)", bot_username)
                    return resp

                if not resp.buttons:
                    # این پیام هیچ دکمه‌ای (نه اینلاین نه کیبورد معمولی) نداره؛
                    # پس اصلاً معنی نداره دنبال دکمه‌ی عضویت بگردیم. اول ببینیم
                    # آیا خودِ این پیام یه دیپ‌لینکِ بعدی (زنجیره‌ای) داده یا نه.
                    next_link = find_start_link_in_message(resp)
                    if next_link and next_link not in visited:
                        log.info(
                            "deeplink: %s -> این پیام دکمه نداشت، مستقیم رفت سراغ لینک بعدی: %s",
                            bot_username, next_link,
                        )
                        current_link = next_link
                        advanced_to_next_hop = True
                        break

                    # نه فایل، نه دکمه، نه لینکی تو متن؛ شاید فقط یه پیام
                    # میانیه (مثلاً «صبر کن...») و پیام واقعی چند ثانیه دیگه میاد.
                    log.info(
                        "deeplink: %s -> نه فایل، نه دکمه، نه لینکی پیدا شد؛ متن: %r؛ تا ۱۵ ثانیه منتظر پیام بعدی می‌مونیم",
                        bot_username, resp.raw_text,
                    )
                    try:
                        resp = await conv.get_response(timeout=15)
                        continue
                    except asyncio.TimeoutError:
                        log.info("deeplink: %s -> پیام دیگه‌ای نیومد", bot_username)
                        if unresolved_join_links:
                            raise RuntimeError(
                                f"بعد از باز کردن «{bot_username}»، فایل نیومد. این لینک(های) عضویت جوین نشدن "
                                f"(احتمالاً منقضی/نامعتبرن): {', '.join(unresolved_join_links)}"
                            )
                        raise RuntimeError(f"بعد از باز کردن «{bot_username}»، نه فایل اومد نه لینک/دکمه‌ای پیدا شد.")

                log.info(
                    "deeplink: %s -> تلاش %s/%s: فایل هنوز نیومده، دنبال دکمه‌های عضویت می‌گردم",
                    bot_username, attempt + 1, max_inner_retries,
                )
                for row in resp.buttons:
                    for btn in row:
                        log.info(
                            "deeplink: %s -> دکمه: متن=«%s» url=%s callback=%s",
                            bot_username, getattr(btn, "text", ""), getattr(btn, "url", None),
                            getattr(btn, "url", None) is None,
                        )

                joined_any = False
                failed_urls = []
                for row in resp.buttons:
                    for btn in row:
                        url = getattr(btn, "url", None)
                        if not url or ("t.me/" not in url and "telegram.me/" not in url):
                            continue
                        # دکمه‌ای که خودش یه دیپ‌لینک دیگه‌ست (start=...) رو اینجا
                        # عضو نمیشیم؛ اون میره تو مرحله‌ی «دنبال کردن لینک بعدی»
                        if parse_start_link(url):
                            log.info(
                                "deeplink: %s -> دکمه‌ی «%s» یه دیپ‌لینکه (%s)، نه کانال، رد میشه از این مرحله",
                                bot_username, getattr(btn, "text", ""), url,
                            )
                            continue
                        # ممکنه این دکمه یه ربات تبلیغاتی باشه که وسط گیت عضویت
                        # قاطی شده، نه یه کانال واقعی؛ قبل از تلاش برای جوین،
                        # مطمئن میشیم واقعاً کانال/گروهه.
                        if not await is_joinable_channel_link(url):
                            log.info(
                                "deeplink: %s -> دکمه‌ی «%s» (%s) کانال/گروه نیست (احتمالاً ربات تبلیغاتیه)، رد میشه",
                                bot_username, getattr(btn, "text", ""), url,
                            )
                            continue
                        log.info("deeplink: %s -> دکمه‌ی عضویت پیدا شد: «%s» -> %s", bot_username, getattr(btn, "text", ""), url)
                        if await join_from_identifier(url, schedule_leave_minutes=gate_leave_minutes):
                            log.info("deeplink: %s -> عضویت در %s موفق", bot_username, url)
                            joined_any = True
                        else:
                            log.warning("deeplink: %s -> عضویت در %s ناموفق", bot_username, url)
                            failed_urls.append(url)

                # بعضی جوین‌ها ممکنه به خاطر فلود‌ویتِ پشت‌سرهم یا تایمینگ
                # موقتاً شکست بخورن؛ یه بار دیگه با کمی مکث امتحان می‌کنیم.
                if failed_urls:
                    await asyncio.sleep(4)
                    still_failed = []
                    for url in failed_urls:
                        log.info("deeplink: %s -> تلاش دوباره برای عضویت در %s", bot_username, url)
                        if await join_from_identifier(url, schedule_leave_minutes=gate_leave_minutes):
                            log.info("deeplink: %s -> عضویت در %s موفق (تلاش دوم)", bot_username, url)
                            joined_any = True
                        else:
                            still_failed.append(url)
                    if still_failed:
                        log.warning(
                            "deeplink: %s -> این لینک‌ها بعد از ۲ تلاش هنوز عضو نشدن (احتمالاً منقضی/نامعتبرن): %s",
                            bot_username, still_failed,
                        )
                        unresolved_join_links.update(still_failed)

                if joined_any:
                    await asyncio.sleep(3)

                # دکمه‌ی «چک عضویت» (بدون url، یه دکمه‌ی callback) رو بزن
                clicked = False
                for row in resp.buttons:
                    for btn in row:
                        if getattr(btn, "url", None) is None:
                            try:
                                log.info("deeplink: %s -> کلیک روی دکمه‌ی تایید عضویت", bot_username)
                                await call_with_flood_retry(
                                    lambda b=btn: b.click(), context=f"کلیک دکمه‌ی تایید عضویت در {bot_username}"
                                )
                                clicked = True
                            except Exception as e:
                                log.warning("click failed for a button in %s: %s", bot_username, e)

                if not joined_any and not clicked:
                    # دکمه بود ولی هیچ‌کدوم قابل استفاده نبودن (نه لینک عضویت
                    # قابل‌تشخیص، نه دکمه‌ی تایید)؛ دیگه کاری از دستمون بر
                    # نمیاد، بریم سراغ چک کردن لینک بعدی توی متن/دکمه‌ها.
                    break

                log.info("deeplink: %s -> دوباره منتظر پاسخ جدید هستیم", bot_username)
                resp = await conv.get_response()

            if advanced_to_next_hop:
                continue

            if resp.document or resp.photo or resp.file:
                log.info("deeplink: %s -> فایل رسید (بدون دانلود، مستقیم فوروارد میشه)", bot_username)
                return resp

            # فایل نیومد؛ شاید این ربات به‌جای گیت/فایل یه دیپ‌لینک دیگه داده
            next_link = find_start_link_in_message(resp)
            if next_link and next_link not in visited:
                log.info("deeplink: %s -> فایل نیومد ولی یه دیپ‌لینک جدید پیدا شد: %s", bot_username, next_link)
                current_link = next_link
                continue

            if unresolved_join_links:
                raise RuntimeError(
                    f"بعد از باز کردن «{bot_username}»، فایل نیومد. این لینک(های) عضویت جوین نشدن "
                    f"(احتمالاً منقضی/نامعتبرن): {', '.join(unresolved_join_links)}"
                )
            raise RuntimeError(f"بعد از باز کردن «{bot_username}»، نه فایل اومد نه لینک جدیدی پیدا شد.")

    raise RuntimeError(f"بعد از {max_hops} مرحله (دنبال کردن لینک‌های زنجیره‌ای)، فایل نهایی گرفته نشد: {link}")


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
    await warm_up_entity_cache()


async def warm_up_entity_cache():
    """چون از StringSession استفاده میکنیم (نه فایل سشن)، کش موجودیت‌های
    Telethon بین هر بار بالا اومدنِ پروسه از صفر شروع میشه. بدون این کش،
    get_entity(آیدی_عددی) برای کانال‌هایی که فقط با آیدی خام (نه یوزرنیم/
    لینک) اضافه شدن شکست میخوره: 'Could not find the input entity'.
    گرفتن کامل دیالوگ‌ها این کش رو برای همه‌ی چت/کانال‌هایی که اکانت
    سشن عضوشونه از نو می‌سازه."""
    try:
        await user.get_dialogs()
        log.info("session entity cache warmed up (get_dialogs)")
    except Exception as e:
        log.warning("could not warm up entity cache: %s", e)


async def resolve_channel_entity(target):
    """target: یوزرنیم/لینک، یا آیدی عددی (خام یا با پیشوند -100). اگه به
    خاطر خالی بودن کش (بعد از ری‌استارت) شکست خورد، یه‌بار کش رو دوباره
    می‌سازه و امتحان میکنه."""
    ident = int(target) if str(target).lstrip("-").isdigit() else target
    try:
        return await user.get_entity(ident)
    except ValueError:
        log.info("resolve_channel_entity: کش پیدا نشد برای %s، در حال بازسازی کش دیالوگ‌ها", target)
        await warm_up_entity_cache()
        return await user.get_entity(ident)


async def process_due_gate_leaves():
    """کانال/گروه‌هایی که موقتاً برای رد کردن یه گیت عضویت جوین شده بودیم و
    زمان لفت‌دادنشون رسیده رو ترک میکنه (کانال‌های مبدا/مقصدِ اصلی هیچوقت
    اینجا نمیان، چون فقط joinهای گیت با schedule_leave_minutes ثبت میشن)."""
    if not session_connected():
        return
    due = db.due_gate_leaves()
    for target in due:
        try:
            entity = await resolve_channel_entity(target)
            await call_with_flood_retry(lambda: user(LeaveChannelRequest(entity)), context=f"ترک {target}")
            log.info("gate-leave: از %s لفت داده شد", target)
        except Exception as e:
            log.warning("gate-leave: نتونستم از %s لفت بدم: %s", target, e)
        finally:
            db.remove_gate_join(target)


async def list_session_channels():
    """کانال‌ها و گروه‌هایی که اکانت سشن عضوشونه رو برمیگردونه (نه چت خصوصی
    و نه ربات‌ها)؛ برای انتخاب از لیست به‌جای وارد کردن دستیِ لینک/آیدی —
    چون این‌ها مستقیم از get_dialogs میان، همیشه توی کشِ Telethon هستن و
    مشکل «Could not find the input entity» براشون پیش نمیاد."""
    if not session_connected():
        return []
    dialogs = await user.get_dialogs()
    channels = []
    seen = set()
    for d in dialogs:
        entity = d.entity
        if isinstance(entity, (Channel, Chat)) and entity.id not in seen:
            seen.add(entity.id)
            channels.append({"id": entity.id, "title": d.name or getattr(entity, "title", str(entity.id))})
    channels.sort(key=lambda c: c["title"] or "")
    return channels


CHANNEL_PICKER_PAGE_SIZE = 6


async def channel_picker_buttons(kind: str, page: int):
    """kind: 'src' یا 'dest'؛ یه صفحه از کانال‌های سشن رو به‌عنوان دکمه میسازه."""
    channels = await list_session_channels()
    start = page * CHANNEL_PICKER_PAGE_SIZE
    page_channels = channels[start:start + CHANNEL_PICKER_PAGE_SIZE]
    rows = [[Button.inline((c["title"] or str(c["id"]))[:60], data=f"{kind}pick:add:{c['id']}")] for c in page_channels]
    nav = []
    if page > 0:
        nav.append(Button.inline("⬅️ قبلی", data=f"{kind}pick:page:{page - 1}"))
    if start + CHANNEL_PICKER_PAGE_SIZE < len(channels):
        nav.append(Button.inline("بعدی ➡️", data=f"{kind}pick:page:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([Button.inline("🔙 بازگشت", data="menu:destchannels" if kind == "dest" else "menu:srcchannels")])
    return rows, len(channels)


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


async def safe_download_media(entity_or_message, **kwargs):
    """دور download_media؛ اگه به باگ شناخته‌شده‌ی Telethon روی بعضی عکس‌های
    فشرده خورد (AttributeError با کلمه‌ی location)، یه خطای خوانا میده."""
    try:
        return await user.download_media(entity_or_message, **kwargs)
    except AttributeError as e:
        if "location" in str(e):
            raise RuntimeError(
                "این عکس با فرمتی ذخیره شده که این نسخه از کتابخونه‌ی Telethon "
                f"نمی‌تونه دانلودش کنه (باگ شناخته‌شده‌ی Telethon): {e}"
            ) from e
        raise


async def is_joinable_channel_link(url: str) -> bool:
    """چک میکنه یه لینک واقعاً به یه کانال/گروه اشاره داره، نه به یه ربات
    (مثلا یه ربات تبلیغاتی که دکمه‌اش وسط گیت عضویت قاطی شده). لینک‌های
    دعوت خصوصی (+hash یا joinchat) همیشه قابل‌عضویت فرض میشن، چون فقط
    برای کانال/گروه صادر میشن، نه ربات."""
    m = INVITE_LINK_RE.search(url or "")
    if not m:
        return False
    if "/+" in url or "joinchat" in url:
        return True
    username = m.group(1)
    try:
        entity = await user.get_entity(username)
    except Exception:
        return False
    return not getattr(entity, "bot", False) and type(entity).__name__ != "User"


async def join_from_identifier(identifier: str, *, schedule_leave_minutes: int = None) -> bool:
    """identifier: لینک t.me، یوزرنیم @، یا آیدی عددی کانالی که سشن از قبل عضوشه.
    schedule_leave_minutes: اگه داده بشه، بعد از یه عضویتِ *واقعی و تازه*
    (نه وقتی از قبل عضو بودیم)، این تعداد دقیقه بعد خودکار از اون کانال/گروه
    لفت داده میشه — برای عضویت‌های موقتِ گیت، نه کانال‌های مبدا/مقصدِ اصلی."""
    m = INVITE_LINK_RE.search(identifier)
    try:
        if m and ("/+" in identifier or "joinchat" in identifier):
            invite_hash = m.group(1)
            # اول چک کن شاید از قبل عضو باشیم؛ بدون این چک، تلاش برای عضویت
            # مجدد با همون لینک دعوت گاهی به‌جای موفقیت ساده، خطا میده.
            try:
                check = await user(CheckChatInviteRequest(invite_hash))
                if isinstance(check, ChatInviteAlready):
                    log.info("join: %s از قبل عضو بودیم، نیازی به عضویت دوباره نیست", identifier)
                    return True  # از قبل عضو بودیم؛ لفت‌دادن براش برنامه‌ریزی نمیشه
            except Exception:
                pass  # چک نشد، برو سراغ عضویت معمولی
            result = await call_with_flood_retry(
                lambda: user(ImportChatInviteRequest(invite_hash)),
                context=f"عضویت در {identifier}",
            )
            joined_id = result.chats[0].id if getattr(result, "chats", None) else None
        elif m:
            result = await call_with_flood_retry(
                lambda: user(JoinChannelRequest(m.group(1))),
                context=f"عضویت در {identifier}",
            )
            joined_id = result.chats[0].id if getattr(result, "chats", None) else None
        elif identifier.startswith("@"):
            result = await call_with_flood_retry(
                lambda: user(JoinChannelRequest(identifier)),
                context=f"عضویت در {identifier}",
            )
            joined_id = result.chats[0].id if getattr(result, "chats", None) else None
        else:
            return True  # آیدی عددی: فرض میکنیم از قبل عضوه، لفت‌دادن براش برنامه‌ریزی نمیشه

        if schedule_leave_minutes and joined_id:
            db.schedule_gate_leave(joined_id, schedule_leave_minutes * 60)
            log.info("join: %s (id=%s) بعد از %s دقیقه خودکار لفت داده میشه", identifier, joined_id, schedule_leave_minutes)
        return True
    except UserAlreadyParticipantError:
        return True
    except InviteHashExpiredError:
        log.warning("join failed for %s: لینک دعوت منقضی/نامعتبره (InviteHashExpiredError)", identifier)
        return False
    except InviteRequestSentError:
        log.warning(
            "join failed for %s: این گروه نیاز به تاییدِ ادمینشون داره (InviteRequestSentError)؛ "
            "درخواست عضویت فرستاده شد ولی هنوز عضو نیستیم", identifier,
        )
        return False
    except Exception as e:
        log.warning("join failed for %s: %s: %s", identifier, type(e).__name__, e)
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
    rows.append([Button.inline("📦 دریافت فایل بکاپ", data="backup:export", style="primary")])
    rows.append([Button.inline("📥 بازیابی از فایل بکاپ", data="backup:import_start", style="primary")])
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
    rows.append([Button.inline("➕ افزودن کانال مقصد (لینک/آیدی)", data="destchannel:add", style="success")])
    rows.append([Button.inline("📋 انتخاب از لیست کانال‌های سشن", data="destpick:page:0", style="primary")])
    rows.append([Button.inline("🔙 بازگشت", data="menu:main")])
    return rows


def src_channels_menu_buttons():
    rows = [
        [Button.inline(f"❌ حذف {c['title'] or c['target']}", data=f"srcchannel:rm:{c['id']}", style="danger")]
        for c in db.list_src_channels()
    ]
    rows.append([Button.inline("➕ افزودن کانال مبدا (لینک/آیدی)", data="srcchannel:add", style="success")])
    rows.append([Button.inline("📋 انتخاب از لیست کانال‌های سشن", data="srcpick:page:0", style="primary")])
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

    if data.startswith("retry:"):
        token = data.split(":", 1)[1]
        ctx = RETRY_CONTEXTS.get(token)
        if not ctx:
            await event.answer("این درخواست منقضی شده (شاید ربات ری‌استارت شده)؛ باید منتظر دور بعدی مانیتور بمونی.", alert=True)
            return
        await event.edit("در حال تلاش مجدد...")
        try:
            await process_source_link(
                ctx["title"], ctx["target"], ctx["source_link"], ctx["source_caption"], ctx["dedup_key"]
            )
            RETRY_CONTEXTS.pop(token, None)
            await event.edit("✅ این بار موفق شد و پست انجام شد.")
        except Exception as e:
            log.warning("retry failed for token %s: %s", token, e, exc_info=True)
            await event.edit(
                f"❌ بازم شکست خورد: {e}",
                buttons=[[Button.inline("🔁 تلاش مجدد", data=f"retry:{token}")]],
            )
        return

    if data == "backup:export":
        await send_backup_file(admin_id, event)
        return

    if data == "backup:import_start":
        PENDING[admin_id] = "restore_backup"
        PENDING_MSG[admin_id] = (event.chat_id, event.message_id)
        await event.edit(
            "فایل بکاپ (JSON) رو همینجا بفرست تا جایگزین همه‌ی تنظیمات فعلی بشه.\n"
            "⚠️ این کار تنظیمات/کانال‌ها/ادمین‌های فعلی رو کامل جایگزین میکنه.",
            buttons=[[Button.inline("انصراف", data="menu:main", style="danger")]],
        )
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

    if data.startswith("srcpick:page:") or data.startswith("destpick:page:"):
        kind = "src" if data.startswith("srcpick:") else "dest"
        page = int(data.split(":")[2])
        if not session_connected():
            await event.answer("اول باید سشن وصل باشه (از پنل «ورود با شماره» یا Session String بزن).", alert=True)
            return
        buttons, total = await channel_picker_buttons(kind, page)
        label = "کانال مبدا" if kind == "src" else "کانال مقصد"
        text = (
            f"یکی از کانال‌ها/گروه‌های زیر رو به‌عنوان {label} انتخاب کن ({total} تا پیدا شد):"
            if total else "هیچ کانال/گروهی توی اکانت سشن پیدا نشد؛ اول باید سشن عضوش باشه."
        )
        await event.edit(text, buttons=buttons)
        return

    if data.startswith("srcpick:add:") or data.startswith("destpick:add:"):
        kind = "src" if data.startswith("srcpick:") else "dest"
        channel_id = int(data.split(":")[2])
        try:
            entity = await resolve_channel_entity(channel_id)
            title = getattr(entity, "title", str(channel_id))
            if kind == "src":
                db.add_src_channel(entity.id, title)
                await event.edit(f"کانال مبدا اضافه شد: {title}", buttons=src_channels_menu_buttons())
            else:
                db.add_dest_channel(entity.id, title)
                await event.edit(f"کانال مقصد اضافه شد: {title}", buttons=dest_channels_menu_buttons())
        except Exception as e:
            await event.answer(f"خطا: {e}", alert=True)
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

    if pending_key == "restore_backup":
        await send_or_edit(
            admin_id, event,
            "منتظر فایل بکاپم، نه متن؛ فایل JSON رو به‌عنوان فایل (نه متن) بفرست.",
            buttons=[[Button.inline("انصراف", data="menu:main", style="danger")]],
        )
        raise events.StopPropagation

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
            entity = await resolve_channel_entity(value)
            db.add_src_channel(entity.id, getattr(entity, "title", value))
            await send_or_edit(admin_id, event, f"کانال مبدا اضافه شد: {getattr(entity, 'title', value)}", buttons=src_channels_menu_buttons())
        except ValueError:
            await send_or_edit(
                admin_id, event,
                "با آیدی عددی خام پیدا نشد. اگه این کانال با یوزرنیم (@channel) یا لینک "
                "دعوتشه، به‌جای آیدی عددی همون رو بفرست؛ چون برای آیدی خام، فقط وقتی "
                "کار میکنه که اکانت سشن قبلاً واقعاً عضو این کانال بوده باشه.",
                buttons=src_channels_menu_buttons(),
            )
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
        min_allowed = 0 if pending_key == "gate_leave_minutes" else 1
        if not value.isdigit() or int(value) < min_allowed:
            label = SETTINGS_LABELS.get(pending_key, pending_key)
            hint = "مثلا 0 یا 30" if pending_key == "gate_leave_minutes" else "مثلا 30"
            await send_or_edit(
                admin_id, event,
                f"{label}\nفقط یه عدد صحیح {'نامنفی' if min_allowed == 0 else 'مثبت'} قبول میشه ({hint})، دوباره بفرست:",
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
async def get_link_from_bot1(file) -> str:
    """file: یا مسیر یه فایل محلی، یا یه پیام تلگرامی حاوی مدیا (Message) —
    توی حالت دوم Telethon خودش فایل رو سمت سرورهای تلگرام کپی میکنه، بدون
    اینکه ما مجبور باشیم دانلودش کنیم و دوباره آپلود کنیم (سریع‌تر و سبک‌تر
    برای سرور)."""
    if not session_connected():
        raise RuntimeError("سشن وصل نیست؛ از پنل روی «ورود با شماره» یا «Session String» بزن")

    bot1 = db.get_setting("bot1_username")
    if not bot1:
        raise RuntimeError("اول از پنل، ربات اول رو تنظیم کن")

    async with user.conversation(bot1, timeout=180) as conv:
        await call_with_flood_retry(lambda: conv.send_file(file), context="ارسال فایل به ربات اول")
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
        trigger_words_display = "، ".join(f"«{w}»" for w in get_trigger_words())
        await status.edit(
            f"لینک نهایی:\n{final_link}\n\n"
            f"حالا کپشن رو بفرست (باید یکی از این‌ها توش باشه تا پست بشه: {trigger_words_display})."
        )
    except Exception as e:
        log.exception("process_file failed")
        await status.edit(f"خطا: {e}")
    finally:
        if file_path and os.path.exists(file_path):
            os.remove(file_path)


CHANNEL_MENTION_RE = re.compile(r"@[A-Za-z][A-Za-z0-9_]{3,31}")


def get_trigger_words() -> list:
    """تنظیم «کلمه‌ی تبدیل به لینک» میتونه چندتا کلمه/عبارت با کاما جدا شده
    باشه (مثلاً «مشاهده, مشاهده فیلم, دانلود🎲»)؛ اینجا لیستش می‌کنیم،
    طولانی‌ترها اول (تا عبارت‌های دقیق‌تر روی حالت‌های کوتاه‌تر اولویت داشته
    باشن، مثلاً «مشاهده فیلم» قبل از «مشاهده»)."""
    raw = db.get_setting("trigger_word") or "مشاهده"
    words = [w.strip() for w in raw.split(",") if w.strip()]
    words.sort(key=len, reverse=True)
    return words or ["مشاهده"]


def find_trigger_word_in_text(text: str):
    """اولین کلمه/عبارتِ تنظیم‌شده که واقعاً توی متن پیدا بشه رو برمیگردونه."""
    if not text:
        return None
    for w in get_trigger_words():
        if w in text:
            return w
    return None


def build_channel_message(raw_text: str, link: str, channel_display: str, trigger_word_setting: str = None) -> str:
    text = raw_text.replace("ایدی کانال", channel_display or "")

    # وقتی کپشن از یه پست کانال مبدا کپی شده (نه دستیِ خودمون)، به‌جای
    # placeholder «ایدی کانال»، ته متن آیدی/یوزرنیم واقعیِ خودِ کانال مبدا
    # هست — گاهی حتی دو بار تکرار شده یا دو آیدی متفاوت (معمولاً توی سه
    # خط آخر پیام). همه‌ی @یوزرنیم‌هایی که توی سه خط آخر پیدا بشن رو با
    # آیدیِ کانال مقصدِ ما جایگزین می‌کنیم تا این برچسب(ها) درست بشه،
    # بدون اینکه به mentionهای احتمالیِ وسط متن (که ربطی به برچسب کانال
    # ندارن) دست بزنیم.
    if channel_display:
        lines = text.split("\n")
        tail_start = max(0, len(lines) - 3)
        tail = "\n".join(lines[tail_start:])
        new_tail = CHANNEL_MENTION_RE.sub(channel_display, tail)
        if new_tail != tail:
            lines[tail_start:] = new_tail.split("\n")
            text = "\n".join(lines)

    trigger = find_trigger_word_in_text(text)
    if trigger:
        text = text.replace(trigger, f'<a href="{link}">{trigger}</a>', 1)
    return text


async def post_to_channel(channel: dict, raw_text: str, link: str):
    """پست کردن توی کانال مقصد با اکانت سشن (یوزربات)، نه ربات.
    ربات اصلی (BotFather) هیچوقت پیام رو نمیفرسته و لازم نیست عضو یا ادمین
    هیچ کانال یا ربات اول/دومی باشه؛ فقط پنل تنظیمات رو نشون میده."""
    if not session_connected():
        raise RuntimeError("سشن وصل نیست؛ از پنل روی «ورود با شماره» یا «Session String» بزن")
    final_text = build_channel_message(raw_text, link, channel["display"])
    name = channel["display"] or channel["target"]
    target = channel["target"]
    peer = int(target) if str(target).lstrip("-").isdigit() else target
    await call_with_flood_retry(
        lambda: user.send_message(peer, final_text, parse_mode="html", link_preview=False),
        context=f"ارسال به {name}",
    )


async def notify_admins(text: str):
    for admin_id in db.list_admins():
        try:
            await bot.send_message(admin_id, text)
        except Exception:
            pass


async def send_backup_file(admin_id: int, event):
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    path = os.path.join(DOWNLOAD_DIR, f"backup_{int(time.time())}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(db.export_backup(), f, ensure_ascii=False, indent=2)
    try:
        await bot.send_file(
            admin_id, path,
            caption=(
                "📦 فایل بکاپ تنظیمات این ربات.\n\n"
                "⚠️ این فایل شامل Session String هم میشه (معادل دسترسی کامل به "
                "اکانت سشن)؛ فقط جایی نگهش‌دار که خودت بهش دسترسی داری.\n\n"
                "برای بازیابی روی یه نصب جدید: از پنل روی «📥 بازیابی از فایل "
                "بکاپ» بزن و همین فایل رو بفرست."
            ),
        )
        await send_or_edit(admin_id, event, "فایل بکاپ فرستاده شد.", buttons=main_menu_buttons())
    finally:
        if os.path.exists(path):
            os.remove(path)
        PENDING_MSG.pop(admin_id, None)


async def handle_restore_upload(event):
    admin_id = event.sender_id
    PENDING.pop(admin_id, None)
    status = await event.reply("در حال خوندن فایل بکاپ...")
    file_path = None
    try:
        file_path = await bot.download_media(event.message)
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or "settings" not in data:
            raise ValueError("این فایل فرمت فایل بکاپ همین ربات رو نداره.")

        db.import_backup(data)

        reconnect_note = ""
        stored_session = db.get_setting("session_string")
        if stored_session:
            try:
                await reconnect_user_client(stored_session)
                reconnect_note = " سشن هم دوباره وصل شد."
            except Exception as e:
                log.warning("could not reconnect session after restore: %s", e)
                reconnect_note = " ⚠️ سشن وصل نشد؛ از پنل دوباره لاگین کن."

        await status.edit(f"✅ بازیابی انجام شد؛ همه‌ی تنظیمات/کانال‌ها/ادمین‌ها جایگزین شدن.{reconnect_note}")
        await bot.send_message(admin_id, "پنل تنظیمات:", buttons=main_menu_buttons())
    except Exception as e:
        log.exception("restore failed")
        await status.edit(f"❌ بازیابی ناموفق بود: {e}")
    finally:
        if file_path and os.path.exists(file_path):
            os.remove(file_path)
        PENDING_MSG.pop(admin_id, None)


async def process_source_link(title: str, target, source_link: str, source_caption: str, dedup_key: str):
    """گرفتن فایل از دیپ‌لینکِ زیر پست کانال مبدا، رد کردن از ربات اول/دوم،
    و پست کردن نهایی؛ هم مسیر اصلی مانیتور و هم دکمه‌ی «تلاش مجدد» از همین
    استفاده میکنن. خطاها رو به بیرون پرتاب میکنه تا caller تصمیم بگیره."""
    log.info("watch: %s -> در حال باز کردن دیپ‌لینک %s", target, source_link)
    file_message = await fetch_file_from_start_link(source_link)
    log.info("watch: %s -> پیام حاوی فایل از دیپ‌لینک گرفته شد", target)

    db.mark_downloaded(dedup_key, target)

    async with session_lock:
        log.info("watch: %s -> در حال ارسال فایل به ربات اول (بدون دانلود)", target)
        link1 = await get_link_from_bot1(file_message)
        log.info("watch: %s -> لینک ربات اول گرفته شد: %s", target, link1)
        log.info("watch: %s -> در حال ارسال لینک به ربات دوم", target)
        final_link = await get_link_from_bot2(link1)
        log.info("watch: %s -> لینک نهایی ربات دوم: %s", target, final_link)

    log.info("watch: %s -> در حال پست به کانال‌های مقصد فعال", target)
    await auto_post_after_watch(title, final_link, source_caption)
    log.info("watch: %s -> پست کامل انجام شد ✅", target)


def register_retry_context(title, target, source_link, source_caption, dedup_key) -> str:
    token = uuid.uuid4().hex[:10]
    RETRY_CONTEXTS[token] = {
        "title": title, "target": target, "source_link": source_link,
        "source_caption": source_caption, "dedup_key": dedup_key,
    }
    return token


async def notify_admins_with_retry(text: str, token: str):
    buttons = [[Button.inline("🔁 تلاش مجدد", data=f"retry:{token}")]]
    for admin_id in db.list_admins():
        try:
            await bot.send_message(admin_id, text, buttons=buttons)
        except Exception:
            pass


async def notify_admins_for_manual_post(source_title: str, source_caption: str, final_link: str):
    """وقتی هیچ‌کدوم از کلمه‌های تنظیم‌شده تو کپشن پیدا نشه، به‌جای پست
    خودکار بدون لینک، از ادمین می‌خوایم خودش متن نهایی (با لینک جاسازی‌شده)
    رو بفرسته. با ریپلای‌کردن رو همین پیام، بدون نیاز به دستور خاصی."""
    text = (
        f"⚠️ توی کپشن پستِ «{source_title}» هیچ‌کدوم از کلمه‌های «کلمه(های) تبدیل به "
        f"لینک» پیدا نشد، پس نمی‌تونم خودکار لینکش کنم.\n\n"
        f"کپشن اصلی:\n{source_caption}\n\n"
        f"لینک نهایی:\n{final_link}\n\n"
        f"روی همین پیام ریپلای کن و متن نهایی (با لینک جاسازی‌شده، مثلاً با هایپرلینک "
        f"کردن یه کلمه) رو بفرست تا دقیقاً همونو توی کانال‌های مقصدِ فعال پست کنم."
    )
    for admin_id in db.list_admins():
        try:
            sent = await bot.send_message(admin_id, text)
            MANUAL_POST_CONTEXTS[(admin_id, sent.id)] = {
                "final_link": final_link, "source_title": source_title,
            }
        except Exception:
            pass


async def handle_manual_post_fix(event, ctx: dict):
    """ادمین با ریپلای، متن نهایی (که خودش لینک رو توش جاسازی کرده) رو
    فرستاده؛ دقیقاً همون متن (با همون فرمت/هایپرلینکی که خودش گذاشته) رو
    با سشن توی همه‌ی کانال‌های مقصدِ فعال پست میکنیم."""
    channels = db.list_enabled_dest_channels()
    if not channels:
        await event.reply("کانال مقصد فعالی وجود نداره؛ از پنل روی «کانال‌های مقصد» بزن.")
        return

    ok, failed = [], []
    for channel in channels:
        name = channel["display"] or channel["target"]
        try:
            target = channel["target"]
            peer = int(target) if str(target).lstrip("-").isdigit() else target
            await call_with_flood_retry(
                lambda: user.send_message(
                    peer, event.raw_text, formatting_entities=event.message.entities, link_preview=False
                ),
                context=f"ارسال دستی به {name}",
            )
            ok.append(name)
        except Exception as e:
            failed.append(f"{name}: {e}")

    lines = []
    if ok:
        lines.append("پست شد توی: " + "، ".join(ok))
    if failed:
        lines.append("ارسال با خطا مواجه شد:\n" + "\n".join(failed))
    await event.reply("\n".join(lines) if lines else "هیچ کانالی برای ارسال پیدا نشد.")


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

    if not find_trigger_word_in_text(template):
        # هیچ‌کدوم از کلمه‌های تنظیم‌شده تو کپشن پیدا نشد؛ به‌جای پست کردن
        # بدون لینک، از ادمین می‌خوایم خودش متن نهایی رو بفرسته.
        await notify_admins_for_manual_post(source_title, template, final_link)
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
    log.info("watch: پروسه‌ی مانیتور کانال‌های مبدا استارت شد")
    while True:
        interval_minutes = int(db.get_setting("watch_interval_minutes") or 60)
        interval = interval_minutes * 60
        max_per_day = int(db.get_setting("watch_max_per_day") or 5)
        try:
            healthy = await session_health_ok()
            if not healthy:
                log.warning("watch: سشن قطع/نامعتبره؛ این دور رد میشه (نه چک کانال مبدا نه لفت گیت)")
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

            if healthy:
                # ترک کانال‌های موقتِ گیت که زمانشون رسیده - فارغ از توقف/فعال
                # بودن ارسال خودکار، چون ربطی به پست کردن نداره.
                await process_due_gate_leaves()

            paused = db.get_setting("auto_paused") == "1"
            if paused:
                log.info("watch: ارسال خودکار از پنل متوقف شده (auto_paused=1)؛ این دور رد میشه")
            if healthy and not paused:
                elapsed = time.time() - db.last_download_time()
                daily_count = db.downloads_in_last_24h()
                if daily_count >= max_per_day:
                    log.info("watch: سقف روزانه (%s) پر شده؛ این دور رد میشه", max_per_day)
                elif elapsed < interval:
                    log.info(
                        "watch: هنوز %.0f ثانیه مونده تا فاصله‌ی %s دقیقه‌ای بین پست‌ها تموم بشه، این دور رد میشه",
                        interval - elapsed, interval_minutes,
                    )
                else:
                    log.info(
                        "watch: شروع دور بررسی کانال‌های مبدا (%s کانال، %.0f ثانیه از آخرین دانلود گذشته)",
                        len(db.list_src_channels()), elapsed,
                    )
                    for ch in db.list_src_channels():
                        try:
                            target = ch["target"]
                            log.info("watch: checking source channel %s (%s)", target, ch.get("title"))
                            entity = await resolve_channel_entity(target)
                            msgs = await user.get_messages(entity, limit=1)
                            if not msgs:
                                log.info("watch: %s has no messages at all", target)
                                continue
                            latest = msgs[0]
                            log.info("watch: %s -> latest message id=%s", target, latest.id)

                            source_link = extract_source_link(latest)
                            log.info("watch: %s -> link in latest message: %s", target, source_link or "پیدا نشد")

                            if not source_link:
                                # فقط پست‌هایی که زیرشون لینک دارن پردازش میشن؛
                                # پستی که خودش مستقیم فایل/عکس چسبیده داره (بدون
                                # لینک) عمداً نادیده گرفته میشه.
                                log.info("watch: %s -> لینکی زیر پست نبود (شاید فایل مستقیم بود)، رد شد", target)
                                continue

                            dedup_key = source_link

                            if db.already_downloaded(dedup_key):
                                log.info("watch: %s -> این پیام قبلاً پردازش شده، رد شد", target)
                                continue

                            source_caption = latest.raw_text or ""
                            title = ch["title"] or str(target)

                            try:
                                await process_source_link(title, target, source_link, source_caption, dedup_key)
                            except Exception as e:
                                # عمداً dedup_key رو مارک نمی‌کنیم؛ یعنی اگه تا دور بعدیِ
                                # مانیتور حل نشه، خودش دوباره امتحان میکنه (نه فقط با
                                # دکمه‌ی تلاش مجدد). با دکمه هم میشه فوری امتحان کرد،
                                # مثلاً بعد از رفع دستیِ مشکل (جوین‌شدنِ خودت تو یه گروه).
                                log.warning("watch: %s -> نتونست فایل رو بگیره/پست کنه: %s", target, e, exc_info=True)
                                token = register_retry_context(title, target, source_link, source_caption, dedup_key)
                                await notify_admins_with_retry(
                                    f"⚠️ نتونستم فایل رو از پستِ «{title}» بگیرم/پست کنم.\n"
                                    f"لینک: {source_link}\nخطا: {e}\n\n"
                                    f"اگه لازمه دستی یه جایی رو جوین شدی، بعدش «تلاش مجدد» رو بزن "
                                    f"(وگرنه خودش تا رسیدنِ نوبتِ بعدیِ مانیتور دوباره امتحان میکنه).",
                                    token,
                                )
                                continue

                            if db.downloads_in_last_24h() >= max_per_day:
                                break
                        except Exception as e:
                            log.warning("watch error for %s: %s", ch.get("target"), e, exc_info=True)
        except Exception:
            log.exception("poll_src_channels loop error")

        await asyncio.sleep(min(interval, 60) if interval > 60 else interval)


# ---------------------------------------------------------------- file intake
@bot.on(events.NewMessage(func=lambda e: e.file is not None))
async def file_handler(event):
    if not is_admin(event.sender_id):
        return
    if PENDING.get(event.sender_id) == "restore_backup":
        await handle_restore_upload(event)
        raise events.StopPropagation
    if event.message.text:
        return
    await process_file(event)


# ---------------------------------------------------------------- caption -> channel post
@bot.on(events.NewMessage(func=lambda e: e.text and not e.text.startswith("/")))
async def template_handler(event):
    if not is_admin(event.sender_id):
        return

    reply_id = event.message.reply_to_msg_id
    if reply_id and (event.sender_id, reply_id) in MANUAL_POST_CONTEXTS:
        ctx = MANUAL_POST_CONTEXTS.pop((event.sender_id, reply_id))
        await handle_manual_post_fix(event, ctx)
        raise events.StopPropagation

    if not find_trigger_word_in_text(event.text):
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
async def poll_src_channels_supervisor():
    """poll_src_channels خودش هر خطای داخلی رو می‌گیره و لاگ می‌کنه، ولی
    این یه لایه‌ی محافظ اضافه‌ست: اگه به هر دلیل غیرمنتظره‌ای کلِ تابع
    (نه فقط یه دور از حلقه‌ش) کرش کنه، به‌جای مردنِ ساکتِ کل مانیتور برای
    همیشه، بعد از ۱۰ ثانیه دوباره راه‌اندازیش می‌کنه."""
    while True:
        try:
            await poll_src_channels()
        except Exception:
            log.exception("poll_src_channels کاملاً کرش کرد؛ ۱۰ ثانیه دیگه دوباره راه‌اندازی میشه")
            await asyncio.sleep(10)


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

    asyncio.create_task(poll_src_channels_supervisor())

    log.info("bot started")
    await bot.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
