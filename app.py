"""
app.py - 量化网格机器人 主入口（完整版）
"""
import asyncio
import os
import traceback
from exchange import ExchangeManager
from bot import QuantBot


async def health_check(reader, writer):
    """HTTP 健康检查响应"""
    writer.write(b"HTTP/1.1 200 OK\r\n\r\nOK")
    await writer.drain()
    writer.close()


async def main():
    port = int(os.getenv("PORT", 10000))
    health_server = await asyncio.start_server(health_check, '0.0.0.0', port)
    print(f"🩺 健康检查服务运行在端口 {port}")

    exchange = ExchangeManager()
    bot = QuantBot(exchange)

    await asyncio.gather(
        bot.run(),
        health_server.serve_forever()
    )


if __name__ == "__main__":
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(main())
    except (KeyboardInterrupt, SystemExit):
        print("🛑 系统退出")
    except Exception:
        traceback.print_exc()
    finally:
        try:
            loop.close()
        except:
            pass