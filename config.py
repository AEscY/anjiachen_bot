# config.py
import os
from dotenv import load_dotenv

load_dotenv()

# --- OKX ---
OKX_API_KEY    = os.environ["OKX_API_KEY"]
OKX_SECRET_KEY = os.environ["OKX_SECRET_KEY"]
OKX_PASSPHRASE = os.environ["OKX_PASSPHRASE"]
OKX_DEMO       = os.getenv("OKX_DEMO", "1") == "1"   # 默认模拟盘

# --- Telegram ---
TG_BOT_TOKEN   = os.environ["TG_BOT_TOKEN"]
TG_ALLOWED_IDS = {int(x) for x in os.getenv("TG_ALLOWED_IDS", "").split(",") if x.strip()}

# --- GitHub 状态持久化 ---
GH_TOKEN       = os.environ["GH_TOKEN"]
GH_REPO        = os.environ["GH_REPO"]               # "user/okx-trader-state"
GH_STATE_BRANCH = os.getenv("GH_STATE_BRANCH", "state")
GH_STATE_PATH  = "state.json"

# --- 交易默认值 ---
DEFAULT_INST_ID = "BTC-USDT"
GRID_DEFAULT = {"minPx": 58000, "maxPx": 62000, "gridNum": 20}
DIP_DEFAULT  = {"buyPct": 0.98, "sellPct": 1.03, "basePx": 60000}

# --- 风控 ---
MAX_POSITION_USDT = 500.0
DAILY_LOSS_LIMIT  = 50.0
MAX_DRAWDOWN_PCT  = 0.10