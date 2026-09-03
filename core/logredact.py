"""
日志脱敏 —— 防止密钥通过第三方库的日志泄露。

背景
----
机器人的 Telegram token 已泄露多次（用户贴日志时暴露），
源头不是我们自己的日志，而是第三方库：

    INFO:httpx:HTTP Request: POST
    https://api.telegram.org/bot8709304949:AAGTdt1b.../getMe

python-telegram-bot 用 INFO 级别打印每一次 HTTP 请求的完整 URL，
而 URL 里带着 token。任何拿到日志的人都能直接操控机器人。

实测：
    未脱敏 → token 出现在日志中: True   ❌
    已脱敏 → token 出现在日志中: False  ✅

设计
----
两层防护：
  1. 精确替换：用环境变量里的真实 token 做字符串替换（最可靠）
  2. 正则兜底：匹配 Telegram token 的固定格式 bot<数字>:<串>
     即便环境变量读取失败、或换了新的 token，也能挡住

同时覆盖交易所 API key / secret / passphrase
（ccxt 在某些错误场景下会把请求详情打进日志）。

安装方式：挂在 root logger 的 handler 上，全局生效。
"""

import logging
import os
import re


# Telegram token 格式：<数字ID>:<35位左右字母数字>
_TG_IN_URL = re.compile(r'(bot\d{6,}):[A-Za-z0-9_-]{20,}')
# 独立的 token（不带 bot 前缀）
_TG_BARE = re.compile(r'\b(\d{8,12}):[A-Za-z0-9_-]{30,}\b')


class RedactFilter(logging.Filter):
    """把日志里的密钥替换为 ***REDACTED***"""

    def __init__(self, name=""):
        super().__init__(name)
        self._secrets = self._collect()
        self._placeholder = "***REDACTED***"

    @staticmethod
    def _collect():
        """收集所有需要脱敏的密钥"""
        vals = []
        for env in ("TG_BOT_TOKEN", "OKX_API_KEY", "OKX_SECRET_KEY",
                    "OKX_PASSPHRASE", "API_KEY", "SECRET_KEY", "PASSPHRASE"):
            v = os.environ.get(env, "")
            # 太短的值不做脱敏，否则会误伤正常文本
            if v and len(v) >= 8:
                vals.append(v)
        return vals

    def filter(self, record):
        try:
            msg = record.getMessage()
        except Exception:
            return True

        orig = msg

        # 1) 精确替换已知密钥
        for s in self._secrets:
            if s in msg:
                msg = msg.replace(s, self._placeholder)

        # 2) 正则兜底：URL 里的 bot<id>:<token>
        msg = _TG_IN_URL.sub(r'\1:***REDACTED***', msg)
        # 3) 正则兜底：裸 token
        msg = _TG_BARE.sub(self._placeholder, msg)

        if msg != orig:
            record.msg = msg
            record.args = ()
        return True


def install_redaction(logger=None):
    """
    安装脱敏过滤器。

    必须挂到 handler 上（而非 logger 上）——
    挂在 logger 上只对该 logger 的直接记录生效，
    子 logger 传播过来的记录不会经过它。
    """
    target = logger if logger is not None else logging.getLogger()
    f = RedactFilter()

    # 挂到 root 及所有现有 handler
    for h in target.handlers:
        if not any(isinstance(x, RedactFilter) for x in h.filters):
            h.addFilter(f)

    # 也给常用的第三方库 logger 单独挂上
    for name in ("httpx", "httpcore", "urllib3", "ccxt",
                 "telegram", "telegram.ext", "apscheduler"):
        lg = logging.getLogger(name)
        for h in lg.handlers:
            if not any(isinstance(x, RedactFilter) for x in h.filters):
                h.addFilter(f)
    return f
