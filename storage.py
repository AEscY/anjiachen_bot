"""
storage.py - 参数持久化（config.json）
"""
import json
from config import logger

CONFIG_FILE = "config.json"
DEFAULTS = {
    "tp_pct": 0.08, "sl_pct": 0.05, "trailing_sl_pct": 0.02,
    "single_order_usdt": 100, "timeframe": "15m", "reserve_bottom": 50,
    "symbols": [], "orderbook_filter": True, "waterfall_breaker": True
}

def load_config():
    try:
        with open(CONFIG_FILE, 'r') as f:
            return {**DEFAULTS, **json.load(f)}
    except:
        return dict(DEFAULTS)

def save_config(cfg):
    try:
        with open(CONFIG_FILE, 'w') as f:
            json.dump(cfg, f)
    except Exception as e:
        logger.error(f"保存配置失败: {e}")
