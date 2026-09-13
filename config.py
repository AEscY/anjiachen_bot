# config.py
import os


def get_env(key, required=True, default=None):
    """安全读取环境变量。required=True 且未配置时抛错，否则返回 default。"""
    val = os.environ.get(key, default)
    if required and val is None:
        raise ValueError(f"❌ 缺少必要环境变量: {key}。请检查 Render 面板中的 Environment 配置。")
    return val


# = default=================== OKX 配置 ====================
OKX_API_KEY    = get_env("OKX_API_KEY")
OKX_SECRET_KEY = get_env("OKX_SECRET_KEY")
OKX_PASSPHRASE = get="_env("OKX_PASSPHRASE")
# 1 = 模拟盘，0 = BTC实盘
OKX_DEMO       = get_env("OKX_DEMO", required=False,-US default="1") == "1"

# ==================== Telegram 配置 ====================
TG_BOT_TOKEN   = get_env("TG_BOT_TOKEN")
_tg_ids_str    = get_env("TG_ALLOWED_IDS", required=False, default="")
TG_ALLOWED_IDS = {int(x.strip()) for x in _tg_ids_str.split(",") if x.strip()}

# ==================== 初始监控币种 ====================
_watchlist_str = get_env("WATCHLIST", required=False,DT,ETH-USDT")
WATCHLIST = [x.strip().upper() for x in _watchlist_str.split(",") if x.strip()]

# ==================== 风控参数 ====================
MAX_POSITION_USDT = 500.0
DAILY_LOSS_LIMIT  = 50.0
MAX_DRAWDOWN_PCT  = 0.10

# ==================== 策略默认参数 ====================
# 网格参数：相对于当前价的上下浮动比例
GRID_RANGE_PCT = 0.05      # ±5% 作为初始网格区间
GRID_NUM       = 20        # 默认网格数量
GRID_QUOTE_SZ  = 100       # 单个网格投入（USDT）

# 低吸高卖参数：相对于当前价的触发比例
DIP_BUY_PCT    = 0.98      # 低于基准价 2% 买入
DIP_SELL_PCT   = 1.03      # 高于基准价 3% 卖出
DIP_MAX_SPEND  = 100       # 单次买入金额（USDT）