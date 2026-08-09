"""
indicators.py - 技术指标计算（基于交易所真实K线）
"""
import pandas as pd
import numpy as np
from config import logger

class TechnicalEngine:
    def __init__(self, exchange):
        self.exchange = exchange

    async def calc(self, symbol, timeframe='15m', limit=50):
        ohlcv = []
        try:
            ohlcv = await self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        except Exception as e:
            logger.warning(f"获取K线异常 ({symbol}): {e}")

        if not ohlcv or len(ohlcv) < 20:
            logger.warning(f"K线数据不足 ({symbol})，尝试使用当前价估算")
            ticker = await self.exchange.fetch_ticker(symbol)
            current_price = ticker.get('last', 0)
            if current_price <= 0:
                current_price = 2000
            return self._fallback(current_price)

        try:
            df = pd.DataFrame(ohlcv, columns=['timestamp','open','high','low','close','volume'])
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
            last_upper = bb_upper.iloc[-1]
            last_lower = bb_lower.iloc[-1]
            last_rsi = rsi.iloc[-1]
            last_atr = atr.iloc[-1]

            if pd.isna(last_upper) or pd.isna(last_lower):
                return self._fallback(current_price)
            if pd.isna(last_rsi):
                last_rsi = 50
            if pd.isna(last_atr):
                last_atr = current_price * 0.01

            result = {
                'bb_upper': float(last_upper),
                'bb_middle': float(current_price),
                'bb_lower': float(last_lower),
                'rsi': float(last_rsi),
                'atr': float(last_atr),
                'bandwidth_pct': float((last_upper - last_lower) / current_price * 100)
            }
            logger.info(f"✅ 真实指标计算成功 {symbol}: 上轨{result['bb_upper']:.1f} 下轨{result['bb_lower']:.1f} RSI{result['rsi']:.0f}")
            return result

        except Exception as e:
            logger.error(f"指标计算异常 ({symbol}): {e}")
            ticker = await self.exchange.fetch_ticker(symbol)
            return self._fallback(ticker.get('last', 0))

    @staticmethod
    def _fallback(price):
        if price <= 0:
            price = 2000
        return {
            'bb_upper': price * 1.03,
            'bb_lower': price * 0.97,
            'bb_middle': price,
            'rsi': 50,
            'atr': price * 0.01,
            'bandwidth_pct': 6.0
        }
