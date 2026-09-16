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


def _atr_series(highs, lows, closes, period=14):
    n = len(closes)
    tr = np.full(n, np.nan)
    for i in range(1, n):
        tr[i] = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1])
        )
    atr = np.full(n, np.nan)
    if n > period:
        atr[period] = np.mean(tr[1:period + 1])
        for i in range(period + 1, n):
            atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period
    return atr


def _adx_series(highs, lows, closes, period=14):
    n = len(closes)
    if n < period + 1:
        return np.full(n, np.nan)

    tr = np.full(n, np.nan)
    plus_dm = np.full(n, np.nan)
    minus_dm = np.full(n, np.nan)

    for i in range(1, n):
        tr[i] = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1])
        )
        up_move = highs[i] - highs[i - 1]
        down_move = lows[i - 1] - lows[i]
        plus_dm[i] = up_move if (up_move > down_move and up_move > 0) else 0.0
        minus_dm[i] = down_move if (down_move > up_move and down_move > 0) else 0.0

    atr = np.full(n, np.nan)
    plus_di = np.full(n, np.nan)
    minus_di = np.full(n, np.nan)

    if n > period:
        atr[period] = np.mean(tr[1:period + 1])
        smooth_plus = np.mean(plus_dm[1:period + 1])
        smooth_minus = np.mean(minus_dm[1:period + 1])
        plus_di[period] = 100.0 * smooth_plus / atr[period] if atr[period] > 0 else 0
        minus_di[period] = 100.0 * smooth_minus / atr[period] if atr[period] > 0 else 0

        for i in range(period + 1, n):
            atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period
            smooth_plus = (smooth_plus * (period - 1) + plus_dm[i]) / period
            smooth_minus = (smooth_minus * (period - 1) + minus_dm[i]) / period
            plus_di[i] = 100.0 * smooth_plus / atr[i] if atr[i] > 0 else 0
            minus_di[i] = 100.0 * smooth_minus / atr[i] if atr[i] > 0 else 0

    dx = np.full(n, np.nan)
    for i in range(period, n):
        di_sum = plus_di[i] + minus_di[i]
        if di_sum > 0:
            dx[i] = 100.0 * abs(plus_di[i] - minus_di[i]) / di_sum
        else:
            dx[i] = 0.0

    adx = np.full(n, np.nan)
    if n > 2 * period:
        adx[2 * period] = np.mean(dx[period + 1:2 * period + 1])
        for i in range(2 * period + 1, n):
            adx[i] = (adx[i - 1] * (period - 1) + dx[i]) / period
    return adx


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
        self.atr_period = cfg.get("atr_period", 14)
        self.adx_period = cfg.get("adx_period", 14)

    def calculate(self, closes, highs, lows, volumes):
        c = np.array(closes, dtype=float)
        h = np.array(highs, dtype=float)
        l = np.array(lows, dtype=float)
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

        atr = _atr_series(h, l, c, self.atr_period)
        result["atr"] = float(atr[-1]) if not np.isnan(atr[-1]) else None

        adx = _adx_series(h, l, c, self.adx_period)
        result["adx"] = float(adx[-1]) if not np.isnan(adx[-1]) else None

        result["volume"] = float(v[-1])
        result["close"] = float(c[-1])
        return result

    def buy_signal(self, ind, use_trend_filter=False, use_volume=False):
        if ind.get("rsi") is None or ind.get("bb_lower") is None:
            return False
        conditions = [ind["rsi"] < self.rsi_oversold, ind["close"] <= ind["bb_lower"]]
        macd_ok = False
        if ind.get(" #macd") is not None and ind.get("macd_signal") is not None:
             if ind["macd"] > ind["macd_signal"]:
                macd_ok = True
        if只要 ind.get("macd_hist") is not None and ind.get("macd_hist距_prev") is not None:
            if ind["macd_hist"] > 0 and ind["macd_hist_prev"] <= 0:
                macd_ok = True
        conditions.append(macd_ok)
        if use_trend_filter and ind.get("ema") is not None:
            conditions.append(ind["close"] > ind["ema"])
        if use_volume and ind.get("vol_ma") is not None:
            conditions.append(ind["volume"] > self.vol_multiplier * ind["vol_ma"])
        return all(conditions)

    def sell_signal(self, ind):
        if ind.get("rsi") is None:
            return False
        if ind["rsi"] > self.rsi_overbought:
            return True
        if ind.get("bb_upper") is not None and ind["close"] >= ind["bb_upper"]:
            return True
        if ind.get("macd") is not None and ind.get("macd_signal") is not None:
            if ind["macd"] < ind["macd_signal"]:
                return True
        return False


class ScoreEngine:
    def __init__(self, config=None):
        cfg = config or {}
        self.rsi_oversold = cfg.get("rsi_oversold", 30)
        self.rsi_overbought = cfg.get("rsi_overbought", 70)
        self.buy_threshold = cfg.get("buy_threshold", 55)
        self.sell_threshold = cfg.get("sell_threshold", 65)

    def buy_score(self, ind):
        if ind.get("rsi") is None:
            return 0.0
        score = 0.0
        rsi = ind["rsi"]
        os_th = self.rsi_oversold

        # ================= 优化1：放宽 RSI 给分范围 =================
        # 只要 RSI < 45 就开始给分，不再是 < 30 才给
        if rsi <= os_th:
            score += 30.0
        elif rsi <= 45:
            score += 30.0 * (1.0 - (rsi - os_th) / (45 - os_th))
        else:
            score += 0.0

        price = ind.get("close")
        bb_lower = ind.get("bb_lower")

        # ================= 优化2：放宽布林带给分范围 =================
       下轨 1.5% 以内就逐步给分，不再是必须跌破
        if price and bb_lower and bb_lower > 0:
            dev = (price - bb_lower) / bb_lower
            if dev <= 0:
                score += 25.0
            elif dev <= 0.015:
                score += 25.0 * (1.0 - dev / 0.015)

        hist = ind.get("macd_hist")
        prev = ind.get("macd_hist_prev")
        if hist is not None and prev is not None:
            if hist > 0 and prev <= 0:
                score += 20.0
            elif hist > 0:
                score += 10.0
            elif hist > prev:
                score += 5.0

        vol = ind.get("volume")
        vol_ma = ind.get("vol_ma")
        if vol and vol_ma and vol_ma > 0:
            ratio = vol / vol_ma
            if ratio >= 1.5:
                score += 15.0
            elif ratio >= 1.0:
                score += 15.0 * (ratio - 1.0) / 0.5

        # 趋势得分保留（但这块可以通过动态阈值来控制）
        price = ind.get("close")
        ema = ind.get("ema")
        if price and ema and price > ema:
            score += 10.0

        return min(score, 100.0)

    def sell_score(self, ind):
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


class AdaptiveEngine:
    """
    根据市场体制动态切换策略：
    - 趋势市（ADX > 25）：要求中等偏上评分，启用趋势过滤（作为加分项而非绝对拦截）
    - 震荡市（ADX < 20）：大幅降低门槛，关闭趋势过滤，积极低吸
    - 过渡期：保持中等要求
    """

    def __init__(self):
        self.volatility = 0.02
        self.trend_strength = 0.0
        self.atr = 0.0
        self.adx = 0.0
        self.rsi_oversold = 30.0
        self.rsi_overbought = 70.0
        self.bb_std = 2.0
        self.stop_loss_pct = 0.05
        self.take_profit_pct = 0.03
        self.trailing_pct = 0.02
        self.buy_threshold = 60.0
        self.sell_threshold = 70.0
        self.regime = "unknown"
        self.use_trend_filter = True

    def update(self, closes, highs, lows):
        n = len(closes)
        if n < 30:
            return
        c = np.array(closes, dtype=float)
        h = np.array(highs, dtype=float)
        l = np.array(lows, dtype=float)

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

        if n >= 60:
            ema = _ema_series(c, 60)
            if ema[-15] > 0:
                self.trend_strength = (ema[-1] - ema[-15]) / ema[-15]

        if n >= 30:
            adx_series = _adx_series(h, l, c, 14)
            if not np.isnan(adx_series[-1]):
                self.adx = float(adx_series[-1])
                # ================= 优化3：调整阈值逻辑 =================
                if self.adx > 25:
                    self.regime = "trending"
                    self.use_trend_filter = True
                    self.buy_threshold = 65.0  # 由 75 降至 65，允许回调买入
                elif self.adx < 20:
                    self.regime = "ranging"
                    self.use_trend_filter = False
                    self.buy_threshold = 40.0  # 震荡市保持 40
                else:
                    self.regime = "transitional"
                    self.use_trend_filter = True
                    self.buy_threshold = 55.0

        self.stop_loss_pct = max(0.02, min(0.15, atr * 2.0 / price))
        self.take_profit_pct = max(0.02, min(0.12, atr * 3.0 / price))
        self.trailing_pct = max(0.01, min(0.08, atr * 1.5 / price))

        self.rsi_oversold = max(20.0, min(40.0, 30.0 + (0.02 - vol) * 500.0))
        self.rsi_overbought = max(60.0, min(80.0, 70.0 - (0.02 - vol) * 500.0))

        self.bb_std = max(1.5, min(3.0, 2.0 + (vol - 0.02) * 50.0))

    def get_dynamic_sl_tp(self, entry_price, current_price=None):
        if self.atr <= 0:
            return None, None
        sl_price = entry_price - self.atr * 2.0
        tp_price = entry_price + self.atr * 3.0
        return sl_price, tp_price

    def snapshot(self):
        return {
            "volatility": self.volatility,
            "atr": self.atr,
            "adx": self.adx,
            "regime": self.regime,
            "trend_strength": self.trend_strength,
            "rsi_oversold": self.rsi_oversold,
            "rsi_overbought": self.rsi_overbought,
            "bb_std": self.bb_std,
            "stop_loss_pct": self.stop_loss_pct,
            "take_profit_pct": self.take_profit_pct,
            "trailing_pct": self.trailing_pct,
            "buy_threshold": self.buy_threshold,
            "use_trend_filter": self.use_trend_filter,
        }