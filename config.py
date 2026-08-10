"""
config.py - 全局配置中心
使用 Pydantic Settings 管理所有环境变量，内置日志脱敏过滤器。
"""
import os
import logging
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


# ---------- 日志脱敏过滤器（修复：清空 args 防止 TypeError）----------
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
        # ✅ 修复：必须清空 args，防止 logging.Formatter 二次格式化时报错
        record.args = ()
        return True


# ---------- 日志系统配置 ----------
logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)
logger.addFilter(SensitiveFilter())

# 为 root logger 也添加过滤器，让所有模块的日志都脱敏
root_logger = logging.getLogger()
root_logger.addFilter(SensitiveFilter())


# ---------- Pydantic 配置类（自动加载 .env，自动类型转换）----------
class Settings(BaseSettings):
    """
    所有配置项均从环境变量读取，支持 .env 文件。
    使用 Pydantic 原生类型转换，无需手写 os.getenv。
    """
    
    # 通用配置
    TG_BOT_TOKEN: str = Field(default="", description="Telegram Bot Token")
    TG_CHAT_ID: str = Field(default="", description="Telegram 通知接收 ID")
    IS_SANDBOX: bool = Field(default=True, description="是否为模拟盘模式")
    SYMBOL: str = Field(default="ETH/USDT", description="默认交易对")
    PORT: int = Field(default=10000, description="Web 服务端口")
    ALLOWED_USERS: str = Field(default="", description="白名单用户 ID（逗号分隔）")
    WEBHOOK_URL: str = Field(default="", description="Render Webhook URL")

    # 交易所选择
    EXCHANGE_NAME: str = Field(default="okx", description="交易所名称")

    # OKX 专用凭证
    OKX_API_KEY: str = Field(default="", description="OKX API Key")
    OKX_SECRET_KEY: str = Field(default="", description="OKX Secret Key")
    OKX_PASSPHRASE: str = Field(default="", description="OKX Passphrase")

    # 通用凭证（可替代交易所专用变量）
    API_KEY: str = Field(default="", description="通用 API Key")
    SECRET_KEY: str = Field(default="", description="通用 Secret Key")
    PASSWORD: str = Field(default="", description="通用 Passphrase")

    # 手续费与保本参数
    TAKER_FEE: float = Field(default=0.001, description="吃单费率（默认 0.1%）")
    MAKER_FEE: float = Field(default=0.0008, description="挂单费率（默认 0.08%）")
    MIN_PROFIT_MARGIN: float = Field(default=0.001, description="最小安全垫（默认 0.1%）")

    # ---------- 便捷属性 ----------
    @property
    def allowed_users_list(self) -> list[int]:
        """将逗号分隔的用户 ID 字符串转换为整数列表"""
        if not self.ALLOWED_USERS:
            return []
        return [int(x.strip()) for x in self.ALLOWED_USERS.split(",") if x.strip().isdigit()]

    # ---------- Pydantic 配置 ----------
    model_config = SettingsConfigDict(
        env_file=".env",          # 自动加载 .env 文件
        env_file_encoding="utf-8",
        extra="ignore",           # 忽略多余的 env 变量
        case_sensitive=False,     # 环境变量名不区分大小写
    )


# ---------- 全局单例 ----------
settings = Settings()