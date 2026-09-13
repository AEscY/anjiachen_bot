import os

def get_env(key, required=True, default=None):
    val = os.environ.get(key, default)
    if required and val is None:
        raise ValueError(f"缺少环境变量: {key}")
    return val

OKX_API_KEY = get_env("OKX_API_KEY")
OKX_SECRET_KEY = get_env("OKX_SECRET_KEY")
OKX_PASSPHRASE = get_env("OKX_PASSPHRASE")
OKX_DEMO = get_env("OKX_DEMO", required=False, default="1") == "1"

TG_BOT_TOKEN = get_env("TG_BOT_TOKEN")
_tg_ids_str = get_env("TG_ALLOWED_IDS", required=False, default="")
TG_ALLOWED_IDS = {int(x.strip()) for x in _tg_ids_str.split(",") if x.strip()}

_watchlist_str = get_env("WATCHLIST", required=False, default="BTC-USDT,ETH-USDT")
WATCHLIST = [x.strip().upper() for x in _watchlist_str.split(",") if x.strip()]

MAX_POSITION_USDT = 500.0
DAILY_LOSS_LIMIT = 50.0
MAX_DRAWDOWN_PCT = 0.10

GRID_RANGE_PCT = 0.05
GRID_NUM = 20
GRID_QUOTE_SZ = 100

DIP_BUY_PCT = 0.98
DIP_SELL_PCT = 1.03
DIP_MAX_SPEND = 100