"""
app.py - 量化网格机器人 主入口（支持 Render/Railway/Fly.io 云部署）
修复：动态 PORT / 健康检查规范 / asyncio.run 优雅关闭
"""
import asyncio
import os
import signal
import traceback
from exchange import ExchangeManager
from bot import QuantBot


async def health_check(reader, writer):
    """
    标准化 HTTP 健康检查响应
    - 读取请求避免连接重置
    - 返回完整 HTTP 响应头 + Content-Length
    - 捕获 Socket 关闭异常，避免日志污染
    """
    try:
        # 读取请求（防止 Socket 过早关闭导致 BrokenPipeError）
        try:
            await asyncio.wait_for(reader.read(1024), timeout=1.0)
        except (asyncio.TimeoutError, ConnectionResetError):
            pass  # 健康检查探针可能直接断开，忽略

        # 标准 HTTP 200 响应
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
        # 客户端已断开，无需处理
        pass
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


async def main():
    """
    主入口：
    1. 启动健康检查 HTTP 服务（动态端口）
    2. 启动量化机器人
    3. 优雅关闭信号处理
    """
    # ✅ 修复 1：动态获取 Render/Railway 注入的 PORT 环境变量
    port = int(os.environ.get("PORT", 10000))
    print(f"🩺 健康检查服务将监听端口: {port}")

    # 启动健康检查服务器
    health_server = await asyncio.start_server(health_check, '0.0.0.0', port)
    print(f"✅ 健康检查服务已运行在端口 {port}")

    # 初始化交易所和机器人
    exchange = ExchangeManager()
    bot = QuantBot(exchange)

    # 注册信号处理（优雅关闭）
    shutdown_event = asyncio.Event()

    def signal_handler():
        print("🛑 收到终止信号，正在优雅关闭...")
        shutdown_event.set()

    # 注册 SIGTERM（云平台停止信号）和 SIGINT（Ctrl+C）
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, signal_handler)

    try:
        # 同时运行三个任务
        await asyncio.gather(
            bot.run(),
            health_server.serve_forever(),
            asyncio.Event().wait()  # 占位，实际由信号触发
        )
    except asyncio.CancelledError:
        print("🛑 主任务被取消")
    except Exception as e:
        print(f"❌ 主程序异常: {e}")
        traceback.print_exc()
    finally:
        # ✅ 修复 3：优雅清理
        print("🧹 正在清理资源...")
        health_server.close()
        await health_server.wait_closed()
        await exchange.close()
        print("👋 资源清理完成，程序退出")


def run():
    """程序入口（使用官方推荐的 asyncio.run）"""
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