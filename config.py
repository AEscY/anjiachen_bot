"""
config.py - 全局配置中心（多交易所支持）
"""
import os, logging
from pydantic_settings import BaseSettings

# ---- 日志脱敏 ----
class SensitiveFilter(logging.Filter):
    def filter(self, record):
        msg = record.getMessage()
        for key in ['OKX_API_KEY','OKX_SECRET_KEY','OKX_PASSPHRASE','TG_BOT_TOKEN',
                    'BINANCE_API_KEY','BINANCE_SECRET_KEY']:
            val = os.getenv(key, '')
            if val and len(val) > 8:
                msg = msg.replace(val, val[:4] + '****' + val[-4:])
        record.msg = msg
        return True

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)
logger.addFilter(SensitiveFilter())

class Settings(BaseSettings):
    # 通用
    TG_BOT_TOKEN: str = os.getenv("TG_BOT_TOKEN", "")
    TG_CHAT_ID: str = os.getenv("TG_CHAT_ID", "")
    IS_SANDBOX: bool = os.getenv("IS_SANDBOX", "true").lower() in ("true","1","yes","on")
    SYMBOL: str = os.getenv("SYMBOL", "ETH/USDT")
    PORT: int = int(os.getenv("PORT", "10000"))
    ALLOWED_USERS: str = os.getenv("ALLOWED_USERS", "")

    # 交易所选择 (okx, binance, bybit 等)
    EXCHANGE_NAME: str = os.getenv("EXCHANGE_NAME", "okx").lower()

    # OKX 凭证
    OKX_API_KEY: str = os.getenv("OKX_API_KEY", "")
    OKX_SECRET_KEY: str = os.getenv("OKX_SECRET_KEY", "")
    OKX_PASSPHRASE: str = os.getenv("OKX_PASSPHRASE", "")

    # 通用凭证（可选）
    API_KEY: str = os.getenv("API_KEY", "")
    SECRET_KEY: str = os.getenv("SECRET_KEY", "")
    PASSWORD: str = os.getenv("PASSWORD", "")

settings = Settings()
