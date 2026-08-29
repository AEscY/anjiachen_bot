"""
indicators.py - 技术指标计算（增加趋势强度）
"""
import pandas as pd
import numpy as np
from config import logger

class TechnicalEngine:
    def __init__(self, exchange):
        self.exchange = exchange

    async def calc(self, symbol, timeframe='15m', limit=50, bb_multiplier=2.0):
        ohlcv = await self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        if ohlcv is None or len(ohlcv) < 30:
            raise ValueError(f"K线数据不足: {symbol}")

        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        close = df['close'].astype(float)

        # 布林带
        sma = close.rolling(window=20).mean()
        std = close.rolling(window=20).std()
        bb_upper = sma + bb_multiplier * std
        bb_lower = sma - bb_multiplier * std

        # RSI
        delta = close.diff()
        gain = delta.where(delta > 0, 0.0)
        loss = -delta.where(delta < 0, 0.0)
        avg_gain = gain.ewm(alpha=1/14, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1/14, adjust=False).mean()
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        rsi = rsi.replace([np.inf, -np.inf], 50).fillna(50)

        # ATR
        high = df['high'].astype(float)
        low = df['low'].astype(float)
        prev_close = close.shift(1)
        tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
        atr = tr.ewm(alpha=1/14, adjust=False).mean().fillna(0.0)

        # 趋势强度 (EMA斜率)
        ema50 = close.ewm(span=50, adjust=False).mean()
        trend_strength = (ema50.iloc[-1] - ema50.iloc[-20]) / ema50.iloc[-20] if len(ema50) >= 20 else 0.0

        current_price = close.iloc[-1]
        result = {
            'bb_upper': float(bb_upper.iloc[-1]),
            'bb_middle': float(sma.iloc[-1]),
            'bb_lower': float(bb_lower.iloc[-1]),
            'rsi': float(rsi.iloc[-1]),
            'atr': float(atr.iloc[-1]),
            'bandwidth_pct': float((bb_upper.iloc[-1] - bb_lower.iloc[-1]) / current_price * 100),
            'trend_strength': float(trend_strength),
            'ema50': float(ema50.iloc[-1]),
        }
        return result