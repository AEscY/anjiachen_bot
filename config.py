"""
config.py - 全局配置中心（新增趋势过滤器参数）
"""
import os
import logging
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field

class SensitiveFilter(logging.Filter):
    def filter(self, record):
        msg = record.getMessage()
        for key in ['OKX_API_KEY', 'OKX_SECRET_KEY', 'OKX_PASSPHRASE', 'TG_BOT_TOKEN']:
            val = os.getenv(key, '')
            if val and len(val) > 8:
                msg = msg.replace(val, val[:4] + '****' + val[-4:])
        record.msg = msg
        record.args = ()
        return True

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)
logger.addFilter(SensitiveFilter())
root_logger = logging.getLogger()
root_logger.addFilter(SensitiveFilter())

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