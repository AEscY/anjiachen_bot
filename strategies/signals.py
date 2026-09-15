import numpy as np


def _ema_series(data, period):
    alpha = 2.0 / (period + 1)
    result = np.empty_like(data, dtype=float)
    result[0] = data[0]
    for i in range(1, len(data)):
        result[i] = alpha * data[i] + (1 - alpha) * result[i - 1]
    return result


def _rsi_series(closes, period=14):
    n = len(closes)
    result = np.full(n, np.nan)
    if n <= period:
        return result
    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])
    if avg_loss == 0:
        result[period] = 100.0
    else:
        rs = avg_gain / avg_loss
        result[period] = 100.0 - 100.0 / (1.0 + rs)
    for i in range(period + 1, n):
        avg_gain = (avg_gain * (period - 1) + gains[i - 1]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i - 1]) / period
        if avg_loss == 0:
            result[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            result[i] = 100.0 - 100.0 / (1.0 + rs)
    return result


def _bbands(closes, period=20, std_mult=2.0):
    n = len(closes)
    upper = np.full(n, np.nan)
    middle = np.full(n, np.nan)
    lower = np.full(n, np.nan)
    for i in range(period - 1, n):
        window = closes[i - period + 1:i + 1]
        ma = np.mean(window)
        sd = np.std(window)
        middle[i] = ma
        upper[i] = ma + std_mult * sd
        lower[i] = ma - std_mult * sd
    return upper, middle, lower


def _macd(closes, fast=12, slow=26, signal=9):
    ema_fast = _ema_series(closes, fast)
    ema_slow = _ema_series(closes, slow)
    macd_line = ema_fast - ema_slow
    signal_line = _ema_series(macd_line, signal)
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


class SignalEngine:
    """
    多因子评分引擎。
    买入评分：RSI(30分) + 布林带(25分) + MACD(20分) + 成交量(15分) + 趋势(10分)
    卖出评分：RSI(35分) + 布林带(30分) + MACD(35分)
    """

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
        c = np.array(closes, dtype=float)
        v = np.array(volumes, dtype=float)
        result = {}
        rsi = _rsi_series(c, self.rsi_period)
        result["rsi"] = float(rsi[-1]) if not np.isnan(rsi[-1]) else None
        upper, middle, lower = _bbands(c, self.bb_period, self.bb_std)
        result["bb_upper"] = float(upper[-1]) if not np.isnan(upper[-1]) else None
        result["bb_middle"] = float(middle[-1]) if not np.isnan(middle[-1]) else None
        result["bb_lower"] = float(lower[-1]) if not np.isnan(lower[-1]) else None
        macd_line, signal_line, hist = _macd(c, self.macd_fast, self.macd_slow, self.macd_signal)
        result["macd"] = float(macd_line[-1]) if not np.isnan(macd_line[-1]) else None
        result["macd_signal"] = float(signal_line[-1]) if not np.isnan(signal_line[-1]) else None
        result["macd_hist"] = float(hist[-1]) if not np.isnan(hist[-1]) else None
        result["macd_hist_prev"] = float(hist[-2]) if len(hist) >= 2 and not np.isnan(hist[-2]) else None
        if len(c) >= self.ema_period:
            ema = _ema_series(c, self.ema_period)
            result["ema"] = float(ema[-1])
        else:
            result["ema"] = None
        if len(v) >= self.vol_ma_period:
            vol_ma = np.mean(v[-self.vol_ma_period:])
            result["vol_ma"] = float(vol_ma)
        else:
            result["vol_ma"] = None
        result["volume"] = float(v[-1])
        result["close"] = float(c[-1])
        return result


class ScoreEngine:
    """多因子评分引擎"""

    def __init__(self, config=None):
        cfg = config or {}
        self.rsi_oversold = cfg.get("rsi_oversold", 30)
        self.rsi_overbought = cfg.get("rsi_overbought", 70)
        self.buy_threshold = cfg.get("buy_threshold", 55)
        self.sell_threshold = cfg.get("sell_threshold", 65)

    def buy_score(self, ind) -> float:
        """买入评分 0-100"""
        if ind.get("rsi") is None:
            return 0.0
        score = 0.0
        rsi = ind["rsi"]
        os_th = self.rsi_oversold
        # RSI 30分
        if rsi <= os_th:
            score += 30.0
        elif rsi <= os_th + 10:
            score += 30.0 * (1.0 - (rsi - os_th) / 10.0)
        # 布林带 25分
        price = ind.get("close")
        bb_lower = ind.get("bb_lower")
        if price and bb_lower and bb_lower > 0:
            dev = (price - bb_lower) / bb_lower
            if dev <= 0:
                score += 25.0
            elif dev <= 0.01:
                score += 25.0 * (1.0 - dev / 0.01)
        # MACD 20分
        hist = ind.get("macd_hist")
        prev = ind.get("macd_hist_prev")
        if hist is not None and prev is not None:
            if hist > 0 and prev <= 0:
                score += 20.0
            elif hist > 0:
                score += 10.0
            elif hist > prev:
                score += 5.0
        # 成交量 15分
        vol = ind.get("volume")
        vol_ma = ind.get("vol_ma")
        if vol and vol_ma and vol_ma > 0:
            ratio = vol / vol_ma
            if ratio >= 1.5:
                score += 15.0
            elif ratio >= 1.0:
                score += 15.0 * (ratio - 1.0) / 0.5
        # 趋势 10分
        price = ind.get("close")
        ema = ind.get("ema")
        if price and ema and price > ema:
            score += 10.0
        return min(score, 100.0)

    def sell_score(self, ind) -> float:
        """卖出评分 0-100"""
        if ind.get("rsi") is None:
            return 0.0
        score = 0.0
        rsi = ind["rsi"]
        ob_th = self.rsi_overbought
        if rsi >= ob_th:
            score += 35.0
        elif rsi >= ob_th - 10:
            score += 35.0 * (1.0 - (ob_th - rsi) / 10.0)
        price = ind.get("close")
        bb_upper = ind.get("bb_upper")
        if price and bb_upper and bb_upper > 0:
            dev = (bb_upper - price) / bb_upper
            if dev <= 0:
                score += 30.0
            elif dev <= 0.01:
                score += 30.0 * (1.0 - dev / 0.01)
        macd = ind.get("macd")
        macd_sig = ind.get("macd_signal")
        if macd is not None and macd_sig is not None:
            if macd < macd_sig:
                score += 35.0
        return min(score, 100.0)

    def snapshot(self):
        return {
            "buy_threshold": self.buy_threshold,
            "sell_threshold": self.sell_threshold,
        }