"""
app.py - 量化网格机器人 主入口（支持 Render/Railway/Fly.io 云部署）
修复：动态 PORT / 健康检查规范 / asyncio.run 优雅关闭
增加：详细启动日志，捕获所有异常
"""
import asyncio
import os
import signal
import traceback
import sys
from core.exchange import ExchangeManager
from core.bot import QuantBot

# 强制输出启动信息
print("=" * 50)
print("Starting UltimateBot...")
print(f"Python version: {sys.version}")
print(f"Working directory: {os.getcwd()}")
print(f"Environment variables present: {list(os.environ.keys())}")
print("=" * 50)
sys.stdout.flush()


async def health_check(reader, writer):
    try:
        try:
            await asyncio.wait_for(reader.read(1024), timeout=1.0)
        except (asyncio.TimeoutError, ConnectionResetError):
            pass
        response = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/plain\r\n"
            b"Content-Length: 2\r\n"
            b"Connection: close\r\n"
            b"\r\n"
            b"OK"
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
    try:
        print("🔄 Starting main()...")
        port = int(os.environ.get("PORT", 10000))
        print(f"🩺 Health check port: {port}")

        # 启动健康检查服务器
        health_server = await asyncio.start_server(health_check, '0.0.0.0', port)
        print(f"✅ Health server running on port {port}")

        print("🔌 Initializing exchange...")
        exchange = ExchangeManager()
        print("✅ Exchange initialized")

        print("🤖 Initializing bot...")
        bot = QuantBot(exchange)
        print("✅ Bot initialized")

        # 注册信号处理
        shutdown_event = asyncio.Event()
        def signal_handler():
            print("🛑 Received shutdown signal")
            shutdown_event.set()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, signal_handler)
            except NotImplementedError:
                # Windows 不支持信号处理
                pass

        print("🚀 Starting bot...")
        # 将 bot.run() 放入任务
        bot_task = asyncio.create_task(bot.run())
        health_task = asyncio.create_task(health_server.serve_forever())
        shutdown_task = asyncio.create_task(shutdown_event.wait())

        # 等待任一任务完成或异常
        done, pending = await asyncio.wait(
            [bot_task, health_task, shutdown_task],
            return_when=asyncio.FIRST_COMPLETED
        )

        # 取消所有未完成的任务
        for task in pending:
            task.cancel()
        for task in done:
            if task.exception():
                print(f"❌ Task failed with exception: {task.exception()}")
                traceback.print_exception(task.exception())

        print("🧹 Cleaning up...")
        health_server.close()
        await health_server.wait_closed()
        await exchange.close()
        print("👋 Cleanup done, exiting.")

    except Exception as e:
        print(f"❌ CRITICAL ERROR in main(): {e}")
        traceback.print_exc()
        sys.exit(1)


def run():
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("🛑 User interrupt")
    except Exception as e:
        print(f"❌ Program crashed: {e}")
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    run()