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
print(f"Environment configuration loaded: {'yes' if os.environ else 'no'}")
print("=" * 50)
sys.stdout.flush()


# 全局引用，供健康检查读取机器人真实状态（由 main() 注入）
_BOT = None


def _health_payload():
    """
    构造健康检查响应。

    为什么要分 200 / 503：
      原来的实现对任意路径无条件返回 200 "OK"，
      外部监控只能证明"端口开着"，证明不了"机器人在工作"。
      主循环一旦卡死（假活），监控依然显示健康。

    现在：
      /health  → 真实状态，异常时返回 503，UptimeRobot 能真正报警
      其他路径 → 轻量 200 "OK"，供 Render 存活探测与保活使用

      ⚠️ 不要用 /robots.txt 做保活：Render 对休眠实例会拦截该路径
      直接返回 200 而不唤醒服务，导致监控显示健康但实际在睡。
    """
    if _BOT is None:
        return 200, b"STARTING"
    try:
        st = _BOT.health_status()
    except Exception as e:
        return 503, f"UNHEALTHY health_status error: {e}".encode()

    body = (
        f"{'HEALTHY' if st['healthy'] else 'UNHEALTHY'} "
        f"mode={st['mode']} sandbox={int(st['sandbox'])} "
        f"hb={st['heartbeat_age']} tg={int(st['telegram'])} "
        f"blocked={','.join(st['blocked']) or '-'}"
    ).encode()
    return (200 if st["healthy"] else 503), body


async def health_check(reader, writer):
    try:
        path = b"/"
        try:
            raw = await asyncio.wait_for(reader.read(1024), timeout=1.0)
            if raw:
                head = raw.split(b"\r\n", 1)[0]
                parts = head.split(b" ")
                if len(parts) >= 2:
                    path = parts[1] or b"/"
        except (asyncio.TimeoutError, ConnectionResetError):
            pass

        if path.startswith(b"/health"):
            code, body = _health_payload()
            status = b"200 OK" if code == 200 else b"503 Service Unavailable"
        else:
            # 保活探测：始终 200，确保 Render 不休眠
            code, body, status = 200, b"OK", b"200 OK"

        response = (
            f"HTTP/1.1 {status.decode()}\r\n"
            f"Content-Type: text/plain\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Connection: close\r\n"
            f"\r\n"
        ).encode() + body
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

        # 注入全局引用，让健康检查能读到机器人真实状态
        global _BOT
        _BOT = bot

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
        bot_task = asyncio.create_task(bot.run())
        health_task = asyncio.create_task(health_server.serve_forever())
        shutdown_task = asyncio.create_task(shutdown_event.wait())

        done, pending = await asyncio.wait(
            [bot_task, health_task, shutdown_task],
            return_when=asyncio.FIRST_COMPLETED
        )

        for task in pending:
            task.cancel()
        for task in done:
            if task.cancelled():
                continue
            exc = task.exception()
            if exc:
                print(f"❌ Task failed with exception: {exc}")
                traceback.print_exception(exc)

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