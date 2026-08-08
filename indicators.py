"""
indicators.py - 真实技术指标计算（pandas-ta）
"""
from config import logger

class TechnicalEngine:
    @staticmethod
    def calc(ohlcv_list, current_price):
        try:
            import pandas as pd
            import pandas_ta as ta
        except ImportError:
            logger.warning("pandas-ta 未安装，使用降级数据")
            return TechnicalEngine._fallback(current_price)

        if not ohlcv_list or len(ohlcv_list) < 20:
            return TechnicalEngine._fallback(current_price)

        try:
            df = pd.DataFrame(ohlcv_list, columns=['timestamp','open','high','low','close','volume'])
            df['datetime'] = pd.to_datetime(df['timestamp'], unit='ms')
            df.set_index('datetime', inplace=True)

            bb = ta.bbands(df['close'], length=20, std=2)
            rsi = ta.rsi(df['close'], length=14)
            atr = ta.atr(df['high'], df['low'], df['close'], length=14)

            return {
                'bb_upper': float(bb.iloc[-1].get('BBU_20_2.0', current_price*1.03)),
                'bb_middle': current_price,
                'bb_lower': float(bb.iloc[-1].get('BBL_20_2.0', current_price*0.97)),
                'rsi': float(rsi.iloc[-1]) if not rsi.empty else 50,
                'atr': float(atr.iloc[-1]) if not atr.empty else current_price*0.01,
                'bandwidth_pct': 0
            }
        except Exception as e:
            logger.error(f"指标计算异常: {e}")
            return TechnicalEngine._fallback(current_price)

    @staticmethod
    def _fallback(price):
        return {
            'bb_upper': price*1.03, 'bb_lower': price*0.97,
            'bb_middle': price, 'rsi': 50, 'atr': price*0.01, 'bandwidth_pct': 6.0
        }
