"""
app.py - 量化网格机器人 主入口
"""
import asyncio, traceback
from config import settings
from exchange import ExchangeManager
from bot import QuantBot

async def main():
    exchange = ExchangeManager()
    bot = QuantBot(exchange)

    async def health(reader, writer):
        writer.write(b"HTTP/1.1 200 OK\r\n\r\nOK")
        await writer.drain()
        writer.close()
    await asyncio.start_server(health, '0.0.0.0', settings.PORT)

    await bot.start()
    while True:
        await asyncio.sleep(30)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("🛑 退出")
    except Exception:
        traceback.print_exc()
