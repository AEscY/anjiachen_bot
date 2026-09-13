# config.py
import os


def get_env(key, required=True, default=None):
    val = os.environ.get(key, default)
    if required and val is None:
        raise ValueError(f"❌ 缺少必要环境变量: {key}")
    return val


# ==================== OKX ====================
OKX_API_KEY    = get_env("OKX_API_KEY")
OKX_SECRET_KEY = get_env("OKX_SECRET_KEY")
OKX_PASSPHRASE = get_env("OKX_PASSPHRASE")
OKX_DEMO       = get_env("OKX_DEMO", required=False, default="1") == "1"

# ==================== Telegram ====================
TG_BOT_TOKEN   = get_env("TG_BOT_TOKEN")
_tg_ids_str    = get_env("TG_ALLOWED_IDS", required=False, default="")
TG_ALLOWED_IDS = {int(x.strip()) for x in _tg_ids_str.split(",") if x.strip()}

# ==================== 关注币种 ====================
# Render 环境变量 WATCHLIST 可配置初始币种，逗号分隔
_watchlist_str = get_env("WATCHLIST", required=False, default="BTC-USDT,ETH-USDT")
WATCHLIST = [x.strip().upper() for x in _watchlist_str.split(",") if x.strip()]

# ==================== 风控 ====================
MAX_POSITION_USDT = 500.0