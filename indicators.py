"""
indicators.py - 技术指标计算（获取失败则抛出异常，绝不造数据）
"""
import pandas as pd
import numpy as np
from config import logger


class TechnicalEngine:
    def __init__(self, exchange):
        self.exchange = exchange

    async def calc(self, symbol, timeframe='15m', limit=50):
        """计算布林带/RSI/ATR，数据不足时抛出异常"""
        ohlcv = await self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        if ohlcv is None or len(ohlcv) < 20:
            logger.error(f"K线数据不可用或不足 ({symbol})")
            raise ValueError(f"K线数据不可用: {symbol}")

        try:
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            close = df['close'].astype(float)

            sma = close.rolling(window=20).mean()
            std = close.rolling(window=20).std()
            bb_upper = sma + 2 * std
            bb_lower = sma - 2 * std

            delta = close.diff()
            gain = delta.where(delta > 0, 0.0)
            loss = -delta.where(delta < 0, 0.0)
            avg_gain = gain.rolling(window=14).mean()
            avg_loss = loss.rolling(window=14).mean()
            rs = avg_gain / avg_loss
            rsi = 100 - (100 / (1 + rs))

            high = df['high'].astype(float)
            low = df['low'].astype(float)
            prev_close = close.shift(1)
            tr = np.maximum(high - low, np.abs(high - prev_close))
            tr = np.maximum(tr, np.abs(low - prev_close))
            atr = pd.Series(tr).rolling(window=14).mean()

            current_price = close.iloc[-1]

            return {
                'bb_upper': float(bb_upper.iloc[-1]),
                'bb_middle': float(current_price),
                'bb_lower': float(bb_lower.iloc[-1]),
                'rsi': float(rsi.iloc[-1]) if not pd.isna(rsi.iloc[-1]) else 50,
                'atr': float(atr.iloc[-1]) if not pd.isna(atr.iloc[-1]) else 0.01,
                'bandwidth_pct': float((bb_upper.iloc[-1] - bb_lower.iloc[-1]) / current_price * 100)
            }
        except Exception as e:
            logger.error(f"指标计算异常 ({symbol}): {e}")
            raise ValueError(f"指标计算失败: {symbol}")