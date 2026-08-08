"""
app.py - 量化网格机器人 主入口（Webhook 模式）
只需运行此文件即可启动机器人。
"""
import asyncio
import traceback
from config import settings
from exchange import ExchangeManager
from bot import QuantBot

async def main():
    # 初始化交易所连接
    exchange = ExchangeManager()
    # 创建机器人实例
    bot = QuantBot(exchange)
    # 启动机器人（包含 Webhook 和后台任务）
    await bot.run()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("🛑 系统退出")
    except Exception:
        traceback.print_exc()
