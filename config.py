"""
config.py - 全局配置中心（新增趋势过滤器参数）
"""
import os
import re
import logging
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field

class SensitiveFilter(logging.Filter):
    """
    日志脱敏。

    ⚠️ 原实现的挂载方式是错的：
        logger.addFilter(...) 只对该 logger【直接记录】的日志生效。
        子 logger（如 httpx、telegram）的记录是通过【传播】到达
        root handler 的，不会经过 root logger 上的 filter。

        实测后果：python-telegram-bot 用 INFO 打印每一次 HTTP 请求，
        日志里直接出现
            https://api.telegram.org/bot8709304949:AAGTdt1b.../getMe
        token 完整暴露 —— 任何人拿到都能操控机器人。

    正确做法：挂到 handler 上。
    另外原实现保留 val[:4] + '****' + val[-4:]，仍有部分泄露，
    改为完全替换。
    """

    # 无环境变量时也能挡住的兜底规则
    _TG_IN_URL = re.compile(r'(bot\d{6,}):[A-Za-z0-9_-]{20,}')
    _TG_BARE = re.compile(r'\b(\d{8,12}):[A-Za-z0-9_-]{30,}\b')
    PLACEHOLDER = '***REDACTED***'

    def filter(self, record):
        try:
            msg = record.getMessage()
        except Exception:
            return True
        orig = msg
        for key in ['OKX_API_KEY', 'OKX_SECRET_KEY', 'OKX_PASSPHRASE',
                    'TG_BOT_TOKEN']:
            val = os.getenv(key, '')
            if val and len(val) > 8:
                msg = msg.replace(val, self.PLACEHOLDER)
        # 兜底：即便环境变量缺失或 token 换过，也能挡住
        msg = self._TG_IN_URL.sub(r'\1:' + self.PLACEHOLDER, msg)
        msg = self._TG_BARE.sub(self.PLACEHOLDER, msg)
        if msg != orig:
            record.msg = msg
            record.args = ()
        return True


logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s',
                    level=logging.INFO)
logger = logging.getLogger(__name__)


def _install_sensitive_filter():
    """
    安装脱敏。两层防护：

    第一层（关键）：替换 LogRecord 工厂，在【记录创建时】就脱敏。
        这样无论日志最终走哪个 handler、哪个 logger，都必然被处理。
        挂 handler 的方式有漏洞 —— 后来新增的 handler 不会被覆盖。

    第二层：同时挂到现有 handler 上，作为兜底。
    """
    f = SensitiveFilter()

    # ── 第一层：LogRecord 工厂（全局、无条件生效）──
    if not getattr(logging, "_sensitive_factory_installed", False):
        _old_factory = logging.getLogRecordFactory()

        def _factory(*args, **kwargs):
            record = _old_factory(*args, **kwargs)
            try:
                f.filter(record)
            except Exception:
                pass
            return record

        logging.setLogRecordFactory(_factory)
        logging._sensitive_factory_installed = True

    # ── 第二层：挂到现有 handler（兜底）──
    for h in logging.getLogger().handlers:
        if not any(isinstance(x, SensitiveFilter) for x in h.filters):
            h.addFilter(f)
    for name in ('httpx', 'httpcore', 'urllib3', 'ccxt',
                 'ccxt.base.exchange', 'telegram', 'telegram.ext',
                 'telegram.request', 'apscheduler', 'aiohttp'):
        lg = logging.getLogger(name)
        for h in lg.handlers:
            if not any(isinstance(x, SensitiveFilter) for x in h.filters):
                h.addFilter(f)
    return f


_sensitive_filter = _install_sensitive_filter()

class Settings(BaseSettings):
    # 基础
    TG_BOT_TOKEN: str = Field(default="")
    TG_CHAT_ID: str = Field(default="")
    IS_SANDBOX: bool = Field(default=True)
    LIVE_TRADING_CONFIRM: bool = Field(default=False)
    SYMBOL: str = Field(default="ETH/USDT")
    PORT: int = Field(default=10000)
    ALLOWED_USERS: str = Field(default="")
    EXCHANGE_NAME: str = Field(default="okx")
    
    # API Keys
    OKX_API_KEY: str = Field(default="")
    OKX_SECRET_KEY: str = Field(default="")
    OKX_PASSPHRASE: str = Field(default="")
    API_KEY: str = Field(default="")
    SECRET_KEY: str = Field(default="")
    PASSWORD: str = Field(default="")
    
    # 费率
    TAKER_FEE: float = Field(default=0.001)
    MAKER_FEE: float = Field(default=0.0008)
    MIN_PROFIT_MARGIN: float = Field(default=0.001)
    
    # 趋势过滤器
    TREND_FILTER_ENABLED: bool = Field(default=True)
    TREND_EMA_PERIOD: int = Field(default=50)
    TREND_THRESHOLD: float = Field(default=0.02)

    @property
    def allowed_users_list(self) -> list[int]:
        if not self.ALLOWED_USERS:
            return []
        return [int(x.strip()) for x in self.ALLOWED_USERS.split(",") if x.strip().isdigit()]

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

settings = Settings()