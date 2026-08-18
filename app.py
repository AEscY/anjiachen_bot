"""
app.py - 量化网格机器人 主入口（实盘安全版）
- 动态 PORT
- 健康检查含数据库状态
- 优雅关闭
"""
import asyncio
import os
import signal
import traceback
from exchange import ExchangeManager
from bot import QuantBot
from storage import init_db


async def health_check(reader, writer):
    """
    标准化 HTTP 健康检查响应（含数据库状态）
    """
    try:
        try:
            await asyncio.wait_for(reader.read(1024), timeout=1.0)
        except (asyncio.TimeoutError, ConnectionResetError):
            pass

        # 简单检查数据库是否可用
        db_status = "OK"
        try:
            await init_db()
        except Exception as e:
            db_status = f"ERROR: {e}"

        body = f"OK\nDB: {db_status}".encode()
        response = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/plain\r\n"
            f"Content-Length: {len(body)}\r\n".encode()
            b"Connection: close\r\n"
            b"\r\n"
            + body
        )
        writer.write(response)
        await writer.drain()
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


async def main():
    port = int(os.environ.get("PORT", 10000))
    print(f"🩺 健康检查服务将监听端口: {port}")

    health_server = await asyncio.start_server(health_check, '0.0.0.0', port)
    print(f"✅ 健康检查服务已运行在端口 {port}")

    # 初始化数据库（确保表存在）
    await init_db()

    exchange = ExchangeManager()
    bot = QuantBot(exchange)

    shutdown_event = asyncio.Event()

    def signal_handler():
        print("🛑 收到终止信号，正在优雅关闭...")
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, signal_handler)

    try:
        await asyncio.gather(
            bot.run(),
            health_server.serve_forever(),
            asyncio.Event().wait()
        )
    except asyncio.CancelledError:
        print("🛑 主任务被取消")
    except Exception as e:
        print(f"❌ 主程序异常: {e}")
        traceback.print_exc()
    finally:
        print("🧹 正在清理资源...")
        health_server.close()
        await health_server.wait_closed()
        await exchange.close()
        print("👋 资源清理完成，程序退出")


def run():
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("🛑 用户中断")
    except Exception as e:
        print(f"❌ 程序崩溃: {e}")
        traceback.print_exc()
        raise


if __name__ == "__main__":
    run()
