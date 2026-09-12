# config.py
import os

def get_env(key, required=True, default=None):
    """安全读取环境变量，若为必填项且未配置则抛出明确错误"""
    val = os.environ.get(key, default)
    if required and val is None:
        raise ValueError(f"❌ 缺少必要环境变量: {key}。请检查 Render 面板中的 Environment 配置。")
    return val

# ==================== OKX 配置 ====================
OKX_API_KEY    = get_env("OKX_API_KEY")
OKX_SECRET_KEY = get_env("OKX_SECRET_KEY")
OKX_PASSPHRASE = get_env("OKX_PASSPHRASE")
# 1 表示模拟盘，0 表示实盘
OKX_DEMO       = get_env("OKX_DEMO", required=False, default="1") == "1"

# ==================== Telegram 配置 ====================
TG_BOT_TOKEN   = get_env("TG_BOT_TOKEN")
# 处理允许的 Telegram 用户 ID，支持逗号分隔的字符串
_tg_ids_str    = get_env("TG_ALLOWED_IDS", required=False, default="")
TG_ALLOWED_IDS = {int(x.strip()) for x in _tg_ids_str.split(",") if x.strip()}

# ==================== GitHub 状态持久化配置 ====================
GH_TOKEN       = get_env("GH_TOKEN")
GH_REPO        = get_env("GH_REPO")              # 格式如 "user/okx-trader-state"
GH_STATE_BRANCH = get_env("GH_STATE_BRANCH", required=False, default="state")
GH_STATE_PATH  = "state.json"

# ==================== 交易默认值 ====================
DEFAULT_INST_ID = "BTC-USDT"
GRID_DEFAULT = {"minPx": 58000, "maxPx": 62000, "gridNum": 20}
DIP_DEFAULT  = {"buyPct": 0.98, "sellPct": 1.03, "basePx": 60000}

# ==================== 风控参数 ====================
MAX_POSITION_USDT = 500.0
DAILY_LOSS_LIMIT  = 50.0
MAX_DRAWDOWN_PCT  = 0.10