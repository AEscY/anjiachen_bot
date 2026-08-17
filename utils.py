import time
import json
import hashlib
import hmac
import base64
from datetime import datetime, timezone

def okx_sign(timestamp, method, request_path, body, secret_key):
    """OKX V5 签名工具"""
    if isinstance(body, dict):
        body = json.dumps(body)
    message = timestamp + method.upper() + request_path + body
    mac = hmac.new(bytes(secret_key, encoding='utf8'), bytes(message, encoding='utf8'), digestmod='sha256')
    return base64.b64encode(mac.digest()).decode()

def get_timestamp():
    return datetime.now(timezone.utc).isoformat(tzname=None, timespec='milliseconds') + 'Z'

def calculate_imbalance(bids, asks, depth=10):
    """计算订单簿失衡度: (买量 - 卖量) / (买量 + 卖量) 范围[-1, 1]"""
    bid_vol = sum(float(price) * float(vol) for price, vol in bids[:depth])
    ask_vol = sum(float(price) * float(vol) for price, vol in asks[:depth])
    if bid_vol + ask_vol == 0:
        return 0.0
    return (bid_vol - ask_vol) / (bid_vol + ask_vol)

def safe_float(val, default=0.0):
    try:
        return float(val)
    except:
        return default
