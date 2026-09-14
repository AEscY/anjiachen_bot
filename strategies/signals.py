import numpy as np
import talib

class SignalEngine:
    """多因子信号引擎，基于TA-Lib计算技术指标并生成交易信号"""

    def __init__(self, config=None):
        cfg = config or {}
        self.rsi_period = cfg.get("rsi_period", 14)
        self.rsi_oversold = cfg.get("rsi_oversold", 30)
        self.rsi_overbought = cfg.get("rsi_overbought", 70)
        self.bb_period = cfg.get("bb_period", 20)
        self.bb_std = cfg.get("bb_std", 2.0)
        self.macd_fast = cfg.get("macd_fast", 12)
        self.macd_slow = cfg.get("macd_slow", 26)
        self.macd_signal = cfg.get("macd_signal", 9)
        self.ema_period = cfg.get("ema_period", 200)
        self.vol_ma_period = cfg.get("vol_ma_period", 20)
        self.vol_multiplier = cfg.get("vol_multiplier", 1.5)

    def calculate(self, closes, highs, lows, volumes):
        """计算所有指标，返回最新值"""
        c = np.array(closes, dtype=float)
        h = np.array(highs, dtype=float)
        l = np.array(lows, dtype=float)
        v = np.array(volumes, dtype=float)

        result = {}

        # RSI
        rsi = talib.RSI(c, timeperiod=self.rsi_period)
        result["rsi"] = float(rsi[-1]) if not np.isnan(rsi[-1]) else None

        # Bollinger Bands
        upper, middle, lower = talib.BBANDS(
            c, timeperiod=self.bb_period,
            nbdevup=self.bb_std, nbdevdn=self.bb_std
        )
        result["bb_upper"] = float(upper[-1]) if not np.isnan(upper[-1]) else None
        result["bb_middle"] = float(middle[-1]) if not np.isnan(middle[-1]) else None
        result["bb_lower"] = float(lower[-1]) if not np.isnan(lower[-1]) else None

        # MACD
        macd, macd_signal, macd_hist = talib.MACD(
            c, fastperiod=self.macd_fast,
            slowperiod=self.macd_slow, signalperiod=self.macd_signal
        )
        result["macd"] = float(macd[-1]) if not np.isnan(macd[-1]) else None
        result["macd_signal"] = float(macd_signal[-1]) if not np.isnan(macd_signal[-1]) else None
        result["macd_hist"] = float(macd_hist[-1]) if not np.isnan(macd_hist[-1]) else None

        # EMA 趋势过滤
        ema = talib.EMA(c, timeperiod=self.ema_period)
        result["ema"] = float(ema[-1]) if not np.isnan(ema[-1]) else None

        # 成交量均线
        vol_ma = talib.SMA(v, timeperiod=self.vol_ma_period)
        result["vol_ma"] = float(vol_ma[-1]) if not np.isnan(vol_ma[-1]) else None
        result["volume"] = float(v[-1])

        # 最新价格
        result["close"] = float(c[-1])

        return result

    def buy_signal(self, ind, use_trend_filter=False, use_volume=False):
        """判断是否满足买入条件（多因子组合）"""
        if ind["rsi"] is None or ind["bb_lower"] is None:
            return False

        conditions = []

        # 1. RSI 超卖
        conditions.append(ind["rsi"] < self.rsi_oversold)

        # 2. 价格触及布林带下轨
        conditions.append(ind["close"] <= ind["bb_lower"])

        # 3. MACD 确认（MACD > 信号线 或 柱状图由负转正）
        macd_ok = False
        if ind["macd"] is not None and ind["macd_signal"] is not None:
            macd_ok = ind["macd"] > ind["macd_signal"]
        if ind["macd_hist"] is not None and ind["macd_hist"] > 0:
            macd_ok = True
        conditions.append(macd_ok)

        # 4. 趋势过滤（可选）
        if use_trend_filter and ind["ema"] is not None:
            conditions.append(ind["close"] > ind["ema"])

        # 5. 成交量确认（可选）
        if use_volume and ind["vol_ma"] is not None:
            conditions.append(ind["volume"] > self.vol_multiplier * ind["vol_ma"])

        return all(conditions)

    def sell_signal(self, ind):
        """判断是否满足卖出条件（满足任一即可）"""
        if ind["rsi"] is None:
            return False

        # RSI 超买
        if ind["rsi"] > self.rsi_overbought:
            return True

        # 价格触及布林带上轨
        if ind["bb_upper"] is not None and ind["close"] >= ind["bb_upper"]:
            return True

        # MACD 死叉
        if ind["macd"] is not None and ind["macd_signal"] is not None:
            if ind["macd"] < ind["macd_signal"]:
                return True

        return False