# notifier.py
from aiogram import Bot
from config import TG_BOT_TOKEN, TG_ALLOWED_IDS

bot = Bot(token=TG_BOT_TOKEN)

async def alert(text: str):
    for uid in TG_ALLOWED_IDS:
        try:
            await bot.send_message(uid, text, parse_mode="HTML")
        except Exception:
            pass