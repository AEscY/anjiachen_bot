"""
入口。

三件事：日志脱敏、健康检查端口、启动机器人。

日志脱敏必须挂【handler】而不是 logger ——
挂在 logger 上时，httpx / telegram 等子 logger 的记录是
"传播"到 root handler 的，根本不经过 root logger 的 filter，
等于写了没用。这是旧版真实踩过的坑。
"""
import logging
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import config as C


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
    root.handlers.clear()      # 清掉他人可能已挂的 handler，否则漏过滤
    root.addHandler(h)
    root.setLevel(getattr(logging, C.LOG_LEVEL, logging.INFO))

    # 第三方库的 token 也走同一个 handler，同样被脱敏
    for name in ("httpx", "telegram", "telegram.ext", "ccxt", "urllib3"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True
        lg.setLevel(logging.WARNING)


def start_health(get_status) -> None:
    """Render 要求绑定端口，否则认为服务未启动。"""
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = get_status().encode("utf-8")
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


def main() -> None:
    setup_logging()
    log = logging.getLogger("main")

    from bot import TradingBot
    from commands import build_app

    bot = TradingBot()

    def status():
        if not bot.running:
            return "STOPPED\n"
        if bot.risk.reason:
            return f"PAUSED {bot.risk.reason}\n"
        return "OK\n"

    start_health(status)

    log.info(f"启动 | {'模拟盘' if C.SANDBOX else '实盘'} "
             f"| 币种 {', '.join(bot.state['coins']) or '无'}")

    app = build_app(bot)
    app.run_polling(allowed_updates=["message"], drop_pending_updates=True)


if __name__ == "__main__":
    main()
