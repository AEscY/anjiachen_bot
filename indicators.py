"""
indicators.py - 技术指标计算（真实K线版，修复 inf/NaN 处理）
"""
import pandas as pd
import numpy as np
from config import logger


class TechnicalEngine:
    def __init__(self, exchange):
        self.exchange = exchange

    async def calc(self, symbol, timeframe='15m', limit=50, bb_multiplier=2.0):
        ohlcv = await self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit)

        if ohlcv is None or len(ohlcv) < 20:
            logger.error(f"K线数据不可用或不足 ({symbol}): 获取到 {len(ohlcv) if ohlcv else 0} 条")
            raise ValueError(f"K线数据不可用: {symbol}")

        try:
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            close = df['close'].astype(float)

            sma = close.rolling(window=20).mean()
            std = close.rolling(window=20).std()
            bb_upper = sma + bb_multiplier * std
            bb_lower = sma - bb_multiplier * std

            delta = close.diff()
            gain = delta.where(delta > 0, 0.0)
            loss = -delta.where(delta < 0, 0.0)
            avg_gain = gain.ewm(alpha=1/14, adjust=False).mean()
            avg_loss = loss.ewm(alpha=1/14, adjust=False).mean()
            rs = avg_gain / avg_loss
            rsi = 100 - (100 / (1 + rs))
            rsi = rsi.replace([np.inf, -np.inf], 50).fillna(50)

            high = df['high'].astype(float)
            low = df['low'].astype(float)
            prev_close = close.shift(1)
            tr1 = high - low
            tr2 = (high - prev_close).abs()
            tr3 = (low - prev_close).abs()
            tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
            atr = tr.ewm(alpha=1/14, adjust=False).mean().fillna(0.0)

            current_price = close.iloc[-1]
            bb_upper_val = bb_upper.iloc[-1]
            bb_middle_val = sma.iloc[-1]
            bb_lower_val = bb_lower.iloc[-1]
            rsi_val = rsi.iloc[-1]
            atr_val = atr.iloc[-1]

            if not np.isfinite(bb_upper_val) or not np.isfinite(bb_lower_val):
                raise ValueError("布林带计算出无效值")

            result = {
                'bb_upper': float(bb_upper_val),
                'bb_middle': float(bb_middle_val),
                'bb_lower': float(bb_lower_val),
                'rsi': float(rsi_val) if np.isfinite(rsi_val) else 50.0,
                'atr': float(atr_val) if np.isfinite(atr_val) else 0.0,
                'bandwidth_pct': float((bb_upper_val - bb_lower_val) / current_price * 100),
                'bb_multiplier': bb_multiplier
            }
            logger.info(f"✅ 真实指标 {symbol}: 上轨{result['bb_upper']:.1f} 中轨{result['bb_middle']:.1f} 下轨{result['bb_lower']:.1f} RSI{result['rsi']:.0f}")
            return result
        except Exception as e:
            logger.error(f"指标计算异常 ({symbol}): {e}")
            raise ValueError(f"指标计算失败: {symbol}")
