# notifier.py
import logging
from aiogram import Bot
from config import TG_BOT_TOKEN, TG_ALLOWED_IDS

logger = logging.getLogger(__name__)

bot = Bot(token=TG_BOT_TOKEN)


async def alert(text: str):
    """发送告警消息。若未配置 TG_ALLOWED_IDS，则记录日志而不是静默失败。"""
    if not TG_ALLOWED_IDS:
        logger.warning(f"⚠️ TG_ALLOWED_IDS 未配置，告警未发送。消息内容: {text}")
        return

    for uid in TG_ALLOWED_IDS:
        try:
            await bot.send_message(uid, text, parse_mode="HTML")
        except Exception as e:
            logger.error(f"发送告警给 {uid} 失败: {e}")