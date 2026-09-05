"""
入口。

三件事：日志脱敏、健康检查端口、启动机器人。

关键顺序（旧版真实踩过的坑）：
    健康检查端口必须在【最开始】绑定。
    旧写法先创建 TradingBot（内部会连交易所、校验 token），
    任一步失败 → 进程在绑端口前就退出 →
    Render 报 "No open ports detected" → 部署失败。
    现在反过来：先绑端口保住部署，再初始化，
    初始化失败时端口仍在，日志里有明确原因。

日志脱敏必须挂【handler】而不是 logger ——
挂在 logger 上时，httpx / telegram 等子 logger 的记录是
"传播"到 root handler 的，根本不经过 root logger 的 filter，
等于写了没用。
"""
import importlib.metadata
import logging
import os
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, HTTPServer


def _check_versions() -> None:
    """
    python-telegram-bot 20.x 在 Python 3.13+ 上必崩：
        AttributeError: 'Updater' object has no attribute
        '_Updater__polling_cleanup_cb' and no __dict__ for setting new attributes
    因为 Python 3.13 起禁止给 __slots__ 类动态加属性，21.7+ 才修复。

    这个报错极其晦涩，看不出是版本问题。这里提前检查，
    不匹配就直接说明原因和修法。
    """
    if sys.version_info < (3, 13):
        return
    try:
        v = importlib.metadata.version("python-telegram-bot")
    except Exception:
        return
    major = int(v.split(".")[0])
    if major < 21:
        print("=" * 60, flush=True)
        print("版本不兼容", flush=True)
        print("=" * 60, flush=True)
        print(f"  Python        {sys.version.split()[0]}", flush=True)
        print(f"  telegram-bot  {v}", flush=True)
        print(flush=True)
        print("  python-telegram-bot 20.x 不支持 Python 3.13+。", flush=True)
        print("  两个办法（任选其一）：", flush=True)
        print(flush=True)
        print("  A. 降 Python（推荐，走已测路径）", flush=True)
        print("     Render → Environment → 加变量", flush=True)
        print("     PYTHON_VERSION = 3.11.9", flush=True)
        print("     或仓库根目录放 .python-version 文件，内容 3.11.9", flush=True)
        print(flush=True)
        print("  B. 升依赖（requirements.txt 已自动适配）", flush=True)
        print("     当前依赖没装上 21+，检查 requirements.txt", flush=True)
        print("     是否被改过，或构建缓存未清除。", flush=True)
        print("=" * 60, flush=True)


# 必须在 import telegram 之前 —— 崩了就看不到这段说明了
_check_versions()

# config 在 import 时校验必填变量，缺失即退出 —— 这是刻意的
import config as C  # noqa: E402


class RedactFilter(logging.Filter):
    def __init__(self, token: str):
        super().__init__()
        self.token = token

    def filter(self, record: logging.LogRecord) -> bool:
        if not self.token:
            return True
        try:
            text = record.getMessage()
            if self.token in text:
                record.msg = text.replace(self.token, "***REDACTED***")
                record.args = ()
        except Exception:
            pass
        return True


def setup_logging() -> None:
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%m-%d %H:%M:%S")

    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(fmt)
    h.addFilter(RedactFilter(C.TG_TOKEN))

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(h)
    root.setLevel(getattr(logging, C.LOG_LEVEL, logging.INFO))

    for name in ("httpx", "telegram", "telegram.ext", "ccxt", "urllib3"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True
        lg.setLevel(logging.WARNING)


class _Status:
    """健康检查返回值。初始化失败时也能说明原因。"""

    def __init__(self):
        self.text = "BOOTING\n"

    def set(self, t: str):
        self.text = t

    def get(self) -> str:
        return self.text


def start_health(status: _Status) -> None:
    """Render 要求绑定端口，否则认为服务未启动。"""
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = status.get().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    port = int(os.getenv("PORT", "10000"))
    srv = HTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    logging.getLogger(__name__).info(f"健康检查端口 {port}")


def main() -> int:
    setup_logging()
    log = logging.getLogger("main")
    status = _Status()

    # ① 先绑端口 —— 保住部署，后续任何失败都能在日志里看到
    try:
        start_health(status)
    except Exception as e:
        log.error(f"健康检查端口绑定失败: {type(e).__name__}: {e}")
        return 1

    # ② 再初始化机器人
    try:
        from bot import TradingBot
        from commands import build_app

        log.info("正在连接交易所…")
        bot = TradingBot()
        log.info("交易所连接成功")

        def _st():
            if not bot.running:
                return "STOPPED\n"
            if bot.risk.reason:
                return f"PAUSED {bot.risk.reason}\n"
            return "OK\n"

        # 用闭包替换默认状态
        status._bot = bot  # noqa

        def poll():
            while True:
                try:
                    status.set(_st())
                except Exception:
                    pass
                threading.Event().wait(5)

        threading.Thread(target=poll, daemon=True).start()

        log.info(f"启动 | {'模拟盘' if C.SANDBOX else '实盘'} "
                 f"| 币种 {', '.join(bot.state['coins']) or '无'}")

        # ③ 启动 Telegram —— 失败会抛，下面统一捕获
        app = build_app(bot)
        status.set("OK\n")
        app.run_polling(allowed_updates=["message"],
                        drop_pending_updates=True)
        return 0

    except Exception as e:
        status.set(f"FAILED {type(e).__name__}\n")
        log.error("=" * 50)
        log.error(f"启动失败: {type(e).__name__}: {e}")
        log.error("=" * 50)
        log.error("完整堆栈:")
        for line in traceback.format_exc().splitlines():
            log.error(line)
        log.error("=" * 50)
        log.error("端口保持存活，可在 Logs 页面查看上述原因。")
        log.error("修正后重新部署即可。")
        # 保持存活，让 Render 有机会把日志刷出来
        threading.Event().wait(300)
        return 1


if __name__ == "__main__":
    sys.exit(main())
