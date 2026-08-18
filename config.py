"""
config.py - 全局配置中心（实盘安全版）
- 环境变量强制校验
- 日志正则脱敏
- Pydantic Settings 管理
"""
import os
import re
import logging
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field

# ---------- 日志脱敏过滤器（正则全局替换） ----------
class SensitiveFilter(logging.Filter):
    SENSITIVE_PATTERNS = [
        (re.compile(r'(sk-[a-zA-Z0-9]{32,})'), 'sk-****'),
        (re.compile(r'(Bearer\s+)[a-zA-Z0-9\-_]+'), r'\1****'),
        (re.compile(r'(OKX_API_KEY|OKX_SECRET_KEY|OKX_PASSPHRASE)\s*=\s*\S+'), r'\1=****'),
        (re.compile(r'(TG_BOT_TOKEN)\s*=\s*\S+'), r'\1=****'),
        (re.compile(r'(DEEPSEEK_API_KEY)\s*=\s*\S+'), r'\1=****'),
        (re.compile(r'(BINANCE_API_KEY|BINANCE_SECRET_KEY)\s*=\s*\S+'), r'\1=****'),
        (re.compile(r'(BLOCKCHAIR_API_KEY)\s*=\s*\S+'), r'\1=****'),
    ]

    def filter(self, record):
        msg = record.getMessage()
        for pattern, replacement in self.SENSITIVE_PATTERNS:
            msg = pattern.sub(replacement, msg)
        record.msg = msg
        record.args = ()
        return True

logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)
logger.addFilter(SensitiveFilter())
root_logger = logging.getLogger()
root_logger.addFilter(SensitiveFilter())

# ---------- 强制校验必需环境变量 ----------
REQUIRED_ENV_VARS = [
    "OKX_API_KEY", "OKX_SECRET_KEY", "OKX_PASSPHRASE",
    "TG_BOT_TOKEN", "TG_CHAT_ID"
]
missing = [v for v in REQUIRED_ENV_VARS if not os.getenv(v)]
if missing:
    raise RuntimeError(f"❌ 缺少必需的环境变量: {', '.join(missing)}")

# ---------- Pydantic 配置类 ----------
class Settings(BaseSettings):
    TG_BOT_TOKEN: str = Field(default="", description="Telegram Bot Token")
    TG_CHAT_ID: str = Field(default="", description="Telegram 通知接收 ID")
    IS_SANDBOX: bool = Field(default=True, description="是否为模拟盘模式")
    SYMBOL: str = Field(default="ETH/USDT", description="默认交易对")
    PORT: int = Field(default=10000, description="Web 服务端口")
    ALLOWED_USERS: str = Field(default="", description="白名单用户 ID（逗号分隔）")
    WEBHOOK_URL: str = Field(default="", description="Render Webhook URL")
    EXCHANGE_NAME: str = Field(default="okx", description="交易所名称")
    OKX_API_KEY: str = Field(default="", description="OKX API Key")
    OKX_SECRET_KEY: str = Field(default="", description="OKX Secret Key")
    OKX_PASSPHRASE: str = Field(default="", description="OKX Passphrase")
    API_KEY: str = Field(default="", description="通用 API Key")
    SECRET_KEY: str = Field(default="", description="通用 Secret Key")
    PASSWORD: str = Field(default="", description="通用 Passphrase")
    TAKER_FEE: float = Field(default=0.001, description="吃单费率")
    MAKER_FEE: float = Field(default=0.0008, description="挂单费率")
    MIN_PROFIT_MARGIN: float = Field(default=0.001, description="最小安全垫")
    USE_REAL_DATA_ONLY: bool = Field(default=True, description="禁止使用模拟数据")

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
