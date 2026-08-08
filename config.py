"""
config.py - 全局配置中心（多交易所 + 手续费保本）
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

    # 交易所选择
    EXCHANGE_NAME: str = os.getenv("EXCHANGE_NAME", "okx").lower()

    # OKX 凭证
    OKX_API_KEY: str = os.getenv("OKX_API_KEY", "")
    OKX_SECRET_KEY: str = os.getenv("OKX_SECRET_KEY", "")
    OKX_PASSPHRASE: str = os.getenv("OKX_PASSPHRASE", "")

    # 通用凭证
    API_KEY: str = os.getenv("API_KEY", "")
    SECRET_KEY: str = os.getenv("SECRET_KEY", "")
    PASSWORD: str = os.getenv("PASSWORD", "")

    # 手续费与保本设置（可环境变量覆盖）
    TAKER_FEE: float = float(os.getenv("TAKER_FEE", "0.001"))       # 默认现货吃单 0.1%
    MAKER_FEE: float = float(os.getenv("MAKER_FEE", "0.0008"))      # 默认现货挂单 0.08%
    MIN_PROFIT_MARGIN: float = float(os.getenv("MIN_PROFIT_MARGIN", "0.001"))  # 最小净利润率 0.1%

settings = Settings()
