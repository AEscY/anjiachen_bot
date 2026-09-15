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

        macd_line, signal_line, hist = _macd(
            c, self.macd_fast, self.macd_slow, self.macd_signal
        )
        result["macd"] = float(macd_line[-1]) if not np.isnan(macd_line[-1]) else None
        result["macd_signal"] = float(signal_line[-1]) if not np.isnan(signal_line[-1]) else None
        result["macd_hist"] = float(hist[-1]) if not np.isnan(hist[-1]) else None
        result["macd_hist_prev"] = (
            float(hist[-2]) if len(hist) >= 2 and not np.isnan(hist[-2]) else None
        )

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

    def buy_signal(self, ind, use_trend_filter=False, use_volume=False):
        if ind["rsi"] is None or ind["bb_lower"] is None:
            return False
        conditions = []
        conditions.append(ind["rsi"] < self.rsi_oversold)
        conditions.append(ind["close"] <= ind["bb_lower"])
        macd_ok = False
        if ind["macd"] is not None and ind["macd_signal"] is not None:
            if ind["macd"] > ind["macd_signal"]:
                macd_ok = True
        if ind["macd_hist"] is not None and ind["macd_hist_prev"] is not None:
            if ind["macd_hist"] > 0 and ind["macd_hist_prev"] <= 0:
                macd_ok = True
        conditions.append(macd_ok)
        if use_trend_filter and ind["ema"] is not None:
            conditions.append(ind["close"] > ind["ema"])
        if use_volume and ind["vol_ma"] is not None:
            conditions.append(ind["volume"] > self.vol_multiplier * ind["vol_ma"])
        return all(conditions)

    def sell_signal(self, ind):
        if ind["rsi"] is None:
            return False
        if ind["rsi"] > self.rsi_overbought:
            return True
        if ind["bb_upper"] is not None and ind["close"] >= ind["bb_upper"]:
            return True
        if ind["macd"] is not None and ind["macd_signal"] is not None:
            if ind["macd"] < ind["macd_signal"]:
                return True
        return False


class AdaptiveEngine:
    """
    自适应引擎：根据币种波动率和市场趋势动态调整策略参数。

    核心逻辑：
    - 波动率 = ATR(14) / 当前价
    - 高波动币种（如SOL）→ RSI阈值更严格，止损止盈更大
    - 低波动币种（如BTC某些时期）→ RSI阈值更宽松，止损止盈更小
    - 趋势强时提高买入门槛，避免逆势抄底
    """

    def __init__(self):
        self.volatility = 0.02
        self.trend_strength = 0.0
        self.atr = 0.0
        # 自适应参数
        self.rsi_oversold = 30.0
        self.rsi_overbought = 70.0
        self.bb_std = 2.0
        self.stop_loss_pct = 0.05
        self.take_profit_pct = 0.03
        self.trailing_pct = 0.02
        self.buy_threshold = 60.0
        self.sell_threshold = 70.0

    def update(self, closes, highs, lows):
        """根据最新K线更新自适应参数"""
        n = len(closes)
        if n < 30:
            return

        c = np.array(closes, dtype=float)
        h = np.array(highs, dtype=float)
        l = np.array(lows, dtype=float)

        # ATR
        trs = []
        for i in range(1, n):
            tr = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
            trs.append(tr)
        period = min(14, len(trs))
        atr = float(np.mean(trs[-period:]))
        price = float(c[-1])
        if price <= 0:
            return
        self.atr = atr
        vol = atr / price
        self.volatility = vol

        # 趋势强度（EMA60 最近15根斜率）
        if n >= 60:
            ema = _ema_series(c, 60)
            if ema[-15] > 0:
                self.trend_strength = (ema[-1] - ema[-15]) / ema[-15]

        # ============ 动态参数计算 ============
        # RSI 超卖：波动率越低越容易超卖（阈值越高）；波动率越高阈值越低
        # vol=0.01 → 35; vol=0.02 → 30; vol=0.04 → 20
        self.rsi_oversold = max(20.0, min(40.0, 30.0 + (0.02 - vol) * 500.0))
        self.rsi_overbought = max(60.0, min(80.0, 70.0 - (0.02 - vol) * 500.0))

        # 布林带标准差：高波动加大标准差，让下轨更远
        # vol=0.01 → 1.5; vol=0.02 → 2.0; vol=0.04 → 3.0
        self.bb_std = max(1.5, min(3.0, 2.0 + (vol - 0.02) * 50.0))

        # 止损：高波动容忍更大回撤
        # vol=0.02 → 5%; vol=0.04 → 10%
        self.stop_loss_pct = max(0.02, min(0.10, vol * 2.5))

        # 止盈：高波动目标更大
        # vol=0.02 → 3%; vol=0.04 → 6%
        self.take_profit_pct = max(0.02, min(0.08, vol * 1.5))

        # 移动止盈回撤：高波动容忍更大回撤
        self.trailing_pct = max(0.01, min(0.05, vol * 0.8))

        # 买入分数阈值：趋势强时提高门槛
        abs_trend = abs(self.trend_strength)
        if abs_trend > 0.05:
            self.buy_threshold = 75.0
        elif abs_trend > 0.02:
            self.buy_threshold = 65.0
        else:
            self.buy_threshold = 55.0

    def calc_buy_score(self, ind):
        """计算买入分数 0-100"""
        if ind.get("rsi") is None:
            return 0.0
        score = 0.0

        # RSI 得分（最高40）
        rsi = ind["rsi"]
        os_th = self.rsi_oversold
        if rsi <= os_th:
            score += 40.0
        elif rsi <= os_th + 10:
            score += 40.0 * (1.0 - (rsi - os_th) / 10.0)

        # 布林带得分（最高30）
        price = ind.get("close")
        bb_lower = ind.get("bb_lower")
        if price and bb_lower and bb_lower > 0:
            dev = (price - bb_lower) / bb_lower
            if dev <= 0:
                score += 30.0
            elif dev <= 0.01:
                score += 30.0 * (1.0 - dev / 0.01)

        # MACD 得分（最高20）
        hist = ind.get("macd_hist")
        prev = ind.get("macd_hist_prev")
        if hist is not None and prev is not None:
            if hist > 0 and prev <= 0:
                score += 20.0
            elif hist > 0:
                score += 10.0
            elif hist > prev:
                score += 5.0

        # 成交量得分（最高10）
        vol = ind.get("volume")
        vol_ma = ind.get("vol_ma")
        if vol and vol_ma and vol_ma > 0:
            ratio = vol / vol_ma
            if ratio >= 1.5:
                score += 10.0
            elif ratio >= 1.0:
                score += 10.0 * (ratio - 1.0) / 0.5

        return score

    def calc_sell_score(self, ind):
        """计算卖出分数 0-100"""
        if ind.get("rsi") is None:
            return 0.0
        score = 0.0
        rsi = ind["rsi"]
        ob_th = self.rsi_overbought
        if rsi >= ob_th:
            score += 40.0
        elif rsi >= ob_th - 10:
            score += 40.0 * (1.0 - (ob_th - rsi) / 10.0)

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
                score += 20.0

        return score

    def snapshot(self):
        return {
            "volatility": self.volatility,
            "atr": self.atr,
            "trend_strength": self.trend_strength,
            "rsi_oversold": self.rsi_oversold,
            "rsi_overbought": self.rsi_overbought,
            "bb_std": self.bb_std,
            "stop_loss_pct": self.stop_loss_pct,
            "take_profit_pct": self.take_profit_pct,
            "trailing_pct": self.trailing_pct,
            "buy_threshold": self.buy_threshold,
        }