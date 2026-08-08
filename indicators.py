"""
indicators.py - 技术指标计算
使用 pandas 纯手算布林带、RSI、ATR，无需额外安装 TA-Lib。
"""
import pandas as pd
import numpy as np
from config import logger

class TechnicalEngine:
    @staticmethod
    def calc(ohlcv_list, current_price):
        """输入 ccxt 格式的 K 线数据，返回指标字典"""
        if not ohlcv_list or len(ohlcv_list) < 20:
            logger.warning("K线数据不足，使用默认值")
            return TechnicalEngine._fallback(current_price)

        try:
            df = pd.DataFrame(ohlcv_list, columns=['timestamp','open','high','low','close','volume'])
            close = df['close'].astype(float)

            # 布林带 (20,2)
            sma = close.rolling(window=20).mean()
            std = close.rolling(window=20).std()
            bb_upper = sma + 2 * std
            bb_lower = sma - 2 * std

            # RSI 14
            delta = close.diff()
            gain = delta.where(delta > 0, 0.0)
            loss = -delta.where(delta < 0, 0.0)
            avg_gain = gain.rolling(window=14).mean()
            avg_loss = loss.rolling(window=14).mean()
            rs = avg_gain / avg_loss
            rsi = 100 - (100 / (1 + rs))

            # ATR 14
            high = df['high'].astype(float)
            low = df['low'].astype(float)
            prev_close = close.shift(1)
            tr = np.maximum(high - low, np.abs(high - prev_close))
            tr = np.maximum(tr, np.abs(low - prev_close))
            atr = pd.Series(tr).rolling(window=14).mean()

            last_upper = bb_upper.iloc[-1]
            last_lower = bb_lower.iloc[-1]
            last_rsi = rsi.iloc[-1]
            last_atr = atr.iloc[-1]

            if pd.isna(last_upper) or pd.isna(last_lower):
                return TechnicalEngine._fallback(current_price)
            if pd.isna(last_rsi): last_rsi = 50
            if pd.isna(last_atr): last_atr = current_price * 0.01

            return {
                'bb_upper': float(last_upper),
                'bb_middle': current_price,
                'bb_lower': float(last_lower),
                'rsi': float(last_rsi),
                'atr': float(last_atr),
                'bandwidth_pct': float((last_upper - last_lower) / current_price * 100)
            }
        except Exception as e:
            logger.error(f"指标计算异常: {e}")
            return TechnicalEngine._fallback(current_price)

    @staticmethod
    def _fallback(price):
        return {
            'bb_upper': price * 1.03,
            'bb_lower': price * 0.97,
            'bb_middle': price,
            'rsi': 50,
            'atr': price * 0.01,
            'bandwidth_pct': 6.0
        }
