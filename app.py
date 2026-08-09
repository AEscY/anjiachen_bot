"""
app.py - 量化网格机器人 主入口（长轮询 + 健康检查 HTTP）
"""
import asyncio
import traceback
from exchange import ExchangeManager
from bot import QuantBot


async def health_check(reader, writer):
    """极简 HTTP 响应，用于 Render 健康检查"""
    writer.write(b"HTTP/1.1 200 OK\r\n\r\nOK")
    await writer.drain()
    writer.close()


async def main():
    # 启动健康检查 HTTP 服务（Render 需要）
    port = 10000  # Render 默认 PORT 环境变量
    health_server = await asyncio.start_server(health_check, '0.0.0.0', port)
    print(f"🩺 健康检查服务运行在端口 {port}")

    # 启动量化机器人
    exchange = ExchangeManager()
    bot = QuantBot(exchange)

    # 同时运行两个任务
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
        loop.close()