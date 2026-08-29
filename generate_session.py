"""
یه بار روی سیستم خودت (یا وی‌پی‌اس) اجرا کن، با شماره تلفن اکانتی که
میخوای به عنوان سشن (یوزربات) استفاده بشه لاگین کن، و استرینگ خروجی رو
داخل .env جلوی SESSION_STRING بذار.

اجرا: python generate_session.py
"""
import asyncio
from telethon import TelegramClient
from telethon.sessions import StringSession

API_ID = int(input("API_ID: ").strip())
API_HASH = input("API_HASH: ").strip()


async def main():
    async with TelegramClient(StringSession(), API_ID, API_HASH) as client:
        session_str = client.session.save()
        print("\nSESSION_STRING زیر رو کپی کن و داخل .env بذار:\n")
        print(session_str)
        print()


if __name__ == "__main__":
    asyncio.run(main())
