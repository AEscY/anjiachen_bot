# core/utils.py
import math
import numpy as np
from typing import List

def safe_float(value, default=0.0):
    try:
        v = float(value)
        if math.isnan(v) or math.isinf(v):
            return default
        return v
    except (TypeError, ValueError):
        return default

def calculate_indicators(klines: List[List]) -> dict:
    closes = np.array([k[4] for k in klines[-100:]])
    highs = np.array([k[2] for k in klines[-100:]])
    lows = np.array([k[3] for k in klines[-100:]])
    if len(closes) < 20:
        return {'bb_upper': 0, 'bb_middle': 0, 'bb_lower': 0, 'rsi': 50, 'atr': 0, 'bandwidth_pct': 0, 'volatility': 0, 'trend_strength': 0}
    sma = np.mean(closes[-20:])
    std = np.std(closes[-20:])
    bb_upper = sma + 2 * std
    bb_lower = sma - 2 * std
    bandwidth_pct = (bb_upper - bb_lower) / sma if sma != 0 else 0
    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    avg_gain = np.mean(gains[-14:]) if len(gains) >= 14 else 0
    avg_loss = np.mean(losses[-14:]) if len(losses) >= 14 else 0
    rs = avg_gain / avg_loss if avg_loss != 0 else 999
    rsi = 100 - (100 / (1 + rs)) if rs != 999 else 50
    tr_list = []
    for i in range(1, len(highs)):
        hl = highs[i] - lows[i]
        hc = abs(highs[i] - closes[i-1])
        lc = abs(lows[i] - closes[i-1])
        tr_list.append(max(hl, hc, lc))
    atr = np.mean(tr_list[-14:]) if len(tr_list) >= 14 else 0
    volatility = np.std(closes[-20:]) / sma if sma != 0 else 0
    if len(closes) >= 20:
        x = np.arange(len(closes[-20:]))
        slope = np.polyfit(x, closes[-20:], 1)[0]
        trend_strength = abs(slope) / (sma * 0.001)
        trend_strength = min(trend_strength, 1.0)
    else:
        trend_strength = 0.0
    return {'bb_upper': bb_upper, 'bb_middle': sma, 'bb_lower': bb_lower, 'rsi': rsi, 'atr': atr, 'bandwidth_pct': bandwidth_pct, 'volatility': volatility, 'trend_strength': trend_strength}
