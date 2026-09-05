"""
同步推送。

为什么不用 bot._tell()：
    致命错误可能发生在【事件循环启动之前】——
    Exchange 是在 TradingBot.__init__ 里构造的，那时还没有 loop，
    asyncio.get_running_loop() 会抛 RuntimeError，
    结果是最该送达的告警反而送不出去。

所以这里用 urllib 直接调 Telegram API，不引入新依赖，
任何时机都能发。
"""
import json
import logging
import urllib.error
import urllib.request

import config as C

logger = logging.getLogger(__name__)
API = "https://api.telegram.org/bot{token}/sendMessage"


def push(text: str) -> bool:
    """同步发送。失败只记日志，绝不影响主流程。"""
    if len(text) > 4000:
        text = text[:3990] + "\n…（已截断）"
    url = API.format(token=C.TG_TOKEN)
    data = json.dumps({
        "chat_id": C.TG_CHAT_ID,
        "text": text,
        "disable_web_page_preview": True,
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            r.read()
        return True
    except Exception as e:
        # 这里绝不能崩：推送失败不该让机器人起不来
        logger.error(f"推送失败（不影响交易）: {type(e).__name__}: {e}")
        return False
