"""
配置。所有值来自环境变量，缺一即启动失败（不做静默兜底）。
"""
import os
import sys


def _need(key: str) -> str:
    v = os.getenv(key, "").strip()
    if not v:
        sys.exit(f"缺少环境变量: {key}")
    return v


TG_TOKEN = _need("TG_BOT_TOKEN")
TG_CHAT_ID = int(_need("TG_CHAT_ID"))

EXCHANGE_ID = os.getenv("EXCHANGE_ID", "okx").strip().lower()

API_KEY = _need("OKX_API_KEY")
SECRET = _need("OKX_SECRET_KEY")
PASSPHRASE = _need("OKX_PASSPHRASE")

SANDBOX = os.getenv("OKX_SANDBOX", "true").strip().lower() == "true"

STATE_FILE = os.getenv("STATE_FILE", "state.json")

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").strip().upper()
