"""
config.py - 全局配置中心
使用环境变量配置所有参数，支持多交易所。
新增 WEBHOOK_URL 用于 Webhook 模式。
"""
import os
import logging
from pydantic_settings import BaseSettings

# ---------- 日志脱敏过滤器 ----------
class SensitiveFilter(logging.Filter):
    def filter(self, record):
        msg = record.getMessage()
        # 隐藏敏感信息
        for key in ['OKX_API_KEY', 'OKX_SECRET_KEY', 'OKX_PASSPHRASE', 'TG_BOT_TOKEN',
                    'BINANCE_API_KEY', 'BINANCE_SECRET_KEY']:
            val = os.getenv(key, '')
            if val and len(val) > 8:
                msg = msg.replace(val, val[:4] + '****' + val[-4:])
        record.msg = msg
        return True

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)
logger.addFilter(SensitiveFilter())

# ---------- 配置类 ----------
class Settings(BaseSettings):
    # 通用
    TG_BOT_TOKEN: str = os.getenv("TG_BOT_TOKEN", "")
    TG_CHAT_ID: str = os.getenv("TG_CHAT_ID", "")                  # 用于主动发送通知
    IS_SANDBOX: bool = os.getenv("IS_SANDBOX", "true").lower() in ("true", "1", "yes", "on")
    SYMBOL: str = os.getenv("SYMBOL", "ETH/USDT")                  # 默认交易对
    PORT: int = int(os.getenv("PORT", "10000"))                    # Web 服务端口
    ALLOWED_USERS: str = os.getenv("ALLOWED_USERS", "")            # 逗号分隔的白名单用户 ID
    WEBHOOK_URL: str = os.getenv("WEBHOOK_URL", "")                # Render 服务的完整 URL（https://...）

    # 交易所选择
    EXCHANGE_NAME: str = os.getenv("EXCHANGE_NAME", "okx").lower()

    # OKX 凭证
    OKX_API_KEY: str = os.getenv("OKX_API_KEY", "")
    OKX_SECRET_KEY: str = os.getenv("OKX_SECRET_KEY", "")
    OKX_PASSPHRASE: str = os.getenv("OKX_PASSPHRASE", "")

    # 通用凭证（可替代交易所专用变量）
    API_KEY: str = os.getenv("API_KEY", "")
    SECRET_KEY: str = os.getenv("SECRET_KEY", "")
    PASSWORD: str = os.getenv("PASSWORD", "")

    # 手续费与保本参数
    TAKER_FEE: float = float(os.getenv("TAKER_FEE", "0.001"))           # 吃单费率 0.1%
    MAKER_FEE: float = float(os.getenv("MAKER_FEE", "0.0008"))          # 挂单费率 0.08%
    MIN_PROFIT_MARGIN: float = float(os.getenv("MIN_PROFIT_MARGIN", "0.001"))  # 最小安全垫 0.1%

settings = Settings()
