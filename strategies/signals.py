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
        tr[i] = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
    atr = np.full(n, np.nan)
    if n > period:
        atr[period] = np.mean(tr[1:period+1])
        for i in range(period+1, n):
            atr[i] = (atr[i-1] * (period - 1) + tr[i]) / period
    return atr


def _adx_series(highs, lows, closes, period=14):
    n = len(closes)
    if n < period + 1:
        return np.full(n, np.nan)
    tr = np.full(n, np.nan)
    plus_dm = np.full(n, np.nan)
    minus_dm = np.full(n, np.nan)
    for i in range(1, n):
        tr[i] = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
        up_move = highs[i] - highs[i-1]
        down_move = lows[i-1] - lows[i]
        plus_dm[i] = up_move if (up_move > down_move and up_move > 0) else 0.0
        minus_dm[i] = down_move if (down_move > up_move and down_move > 0) else 0.0
    atr = np.full(n, np.nan)
    plus_di = np.full(n, np.nan)
    minus_di = np.full(n, np.nan)
    if n > period:
        atr[period] = np.mean(tr[1:period+1])
        smooth_plus = np.mean(plus_dm[1:period+1])
        smooth_minus = np.mean(minus_dm[1:period+1])
        plus_di[period] = 100.0 * smooth_plus / atr[period] if atr[period] > 0 else 0
        minus_di[period] = 100.0 * smooth_minus / atr[period] if atr[period] > 0 else 0
        for i in range(period+1, n):
            atr[i] = (atr[i-1] * (period - 1) + tr[i]) / period
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
        adx[2*period] = np.mean(dx[period+1:2*period+1])
        for i in range(2*period+1, n):
            adx[i] = (adx[i-1] * (period - 1) + dx[i]) / period
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


class DynamicGridEngine:
    def __init__(self):
        self.atr = 0.0
        self.atr_ma = 0.0
        self.spacing = 0.0
        self.lower = 0.0
        self.upper = 0.0
        self.regime = "unknown"

    def update(self, closes, highs, lows):
        n = len(closes)
        if n < 30:
            return
        c = np.array(closes, dtype=float)
        h = np.array(highs, dtype=float)
        l = np.array(lows, dtype=float)
        atr_series = _atr_series(h, l, c, 14)
        if np.isnan(atr_series[-1]):
            return
        self.atr = float(atr_series[-1])
        valid_atrs = atr_series[~np.isnan(atr_series)]
        if len(valid_atrs) >= 20:
            self.atr_ma = float(np.mean(valid_atrs[-20:]))
        else:
            self.atr_ma = self.atr
        price = float(c[-1])
        if price <= 0:
            return
        self.spacing = 0.5 * self.atr / price
        self.spacing = max(0.003, min(0.03, self.spacing))

        # ====== 修复点：将下限从 2% 降至 0.5%，尊重真实的波动率 ======
        atr_pct = self.atr / price
        half_range = max(0.005, min(0.10, 2.0 * atr_pct))
        self.lower = price * (1 - half_range)
        self.upper = price * (1 + half_range)

        adx_series = _adx_series(h, l, c, 14)
        if not np.isnan(adx_series[-1]):
            adx = float(adx_series[-1])
            if adx > 25:
                self.regime = "trending"
            elif adx < 20:
                self.regime = "ranging"
            else:
                self.regime = "transitional"


class TrailingStopEngine:
    def __init__(self):
        self.atr = 0.0
        self.trailing_distance = 0.0
        self.trailing_active = False
        self.peak_price = 0.0

    def update_atr(self, atr):
        self.atr = atr

    def calc_trailing_distance(self, price):
        if self.atr <= 0 or price <= 0:
            return 0.05
        return 1.5 * self.atr / price

    def check_trailing_stop(self, price, avg_buy_price):
        if avg_buy_price <= 0:
            return None
        profit_pct = (price - avg_buy_price) / avg_buy_price
        if profit_pct <= 0:
            return None
        if price > self.peak_price:
            self.peak_price = price
        trailing_dist = self.calc_trailing_distance(price)
        drawdown = (self.peak_price - price) / self.peak_price
        if profit_pct > 0.005:
            self.trailing_active = True
        if self.trailing_active and drawdown >= trailing_dist:
            return f"追踪止损(回撤{drawdown*100:.2f}%)"
        return None


class AdaptiveEngine:
    def __init__(self):
        self.volatility = 0.02
        self.atr = 0.0
        self.adx = 0.0
        self.regime = "unknown"
        self.stop_loss_pct = 0.05
        self.take_profit_pct = 0.03
        self.trailing_pct = 0.02
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
            tr = max(h[i] - l[i], abs(h[i] - c[i-1]), abs(l[i] - c[i-1]))
            trs.append(tr)
        period = min(14, len(trs))
        atr = float(np.mean(trs[-period:]))
        price = float(c[-1])
        if price <= 0:
            return
        self.atr = atr
        vol = atr / price
        self.volatility = vol
        adx_series = _adx_series(h, l, c, 14)
        if not np.isnan(adx_series[-1]):
            self.adx = float(adx_series[-1])
            if self.adx > 25:
                self.regime = "trending"
                self.use_trend_filter = True
            elif self.adx < 20:
                self.regime = "ranging"
                self.use_trend_filter = False
            else:
                self.regime = "transitional"
                self.use_trend_filter = True
        self.stop_loss_pct = max(0.02, min(0.15, atr * 2.0 / price))
        self.take_profit_pct = max(0.02, min(0.12, atr * 3.0 / price))
        self.trailing_pct = max(0.01, min(0.08, atr * 1.5 / price))

    def snapshot(self):
        return {
            "volatility": self.volatility,
            "atr": self.atr,
            "adx": self.adx,
            "regime": self.regime,
            "stop_loss_pct": self.stop_loss_pct,
            "take_profit_pct": self.take_profit_pct,
            "trailing_pct": self.trailing_pct,
            "use_trend_filter": self.use_trend_filter,
        }