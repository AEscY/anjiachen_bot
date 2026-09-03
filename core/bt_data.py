"""
bt_data.py - 回测数据层
========================

提供两种数据源：

  1. 合成数据（Synthetic）—— 用于【验证回测引擎本身是否正确】
  2. 真实数据（CSV/JSON）—— 用于【评估策略在真实行情下的表现】

──────────────────────────────────────────
为什么必须先有合成数据
──────────────────────────────────────────
回测引擎本身也是代码，也可能有 bug。

如果只拿真实数据跑，得到"净利 +15%"，你无法判断：
  · 策略真的赚钱
  · 还是引擎算错了

正确做法：先造【已知答案】的数据，验证引擎算得对。
例如：
  · 价格恒定不变 → 网格一次都不该成交 → 净利必须为 0
  · 价格完美锯齿、振幅恰好等于间距 → 成交次数可手算
  · 只有手续费没有价差 → 净利必须为负

引擎通过了自证，它跑真实数据的结果才可信。
这是我在这个项目上反复栽跟头学到的：
  「先构造可证伪的检验，让数据说话」
"""
import math
import random


# ════════════════════════════════════════════════════════════
#  数据结构
# ════════════════════════════════════════════════════════════

class Bar:
    """单根 K 线。用 __slots__ 省内存，长序列回测时差别明显。"""
    __slots__ = ("ts", "open", "high", "low", "close", "volume")

    def __init__(self, ts, o, h, l, c, v=0.0):
        self.ts = ts
        self.open = float(o)
        self.high = float(h)
        self.low = float(l)
        self.close = float(c)
        self.volume = float(v)

    def as_tuple(self):
        return (self.ts, self.open, self.high, self.low, self.close,
                self.volume)

    def __repr__(self):
        return (f"Bar(o={self.open:.4f} h={self.high:.4f} "
                f"l={self.low:.4f} c={self.close:.4f})")


# ════════════════════════════════════════════════════════════
#  合成数据生成器
# ════════════════════════════════════════════════════════════

def gen_flat(n=500, price=100.0):
    """
    价格恒定 —— 用于自证：不应产生任何交易，净利必须为 0。

    这是最基础的引擎正确性检验。如果恒定价格还能"赚到钱"，
    说明引擎在凭空造利润，后面所有结论都不可信。
    """
    return [Bar(i * 900_000, price, price, price, price, 1000.0)
            for i in range(n)]


def gen_sawtooth(n=500, base=100.0, amp=0.02, period=20):
    """
    完美锯齿波 —— 可手算成交次数，用于验证网格逻辑。

    amp    : 单边振幅比例（0.02 = ±2%）
    period : 一个完整上下循环的 K 线数

    若网格间距恰好等于 amp，则每个周期应有可预测的成交次数。
    """
    bars = []
    for i in range(n):
        phase = (i % period) / period
        # 0 → 1 → 0 的三角波
        tri = 1.0 - abs(2.0 * phase - 1.0)
        p = base * (1.0 + (tri - 0.5) * 2.0 * amp)
        bars.append(Bar(i * 900_000, p, p, p, p, 1000.0))
    return bars


def gen_trend(n=500, start=100.0, end=150.0, noise=0.002, seed=42):
    """单边趋势（上涨或下跌）—— 验证策略在趋势行情下的表现"""
    rng = random.Random(seed)
    bars = []
    for i in range(n):
        t = i / max(1, n - 1)
        mid = start + (end - start) * t
        jitter = rng.gauss(0, noise)
        p = mid * (1.0 + jitter)
        bars.append(Bar(i * 900_000, p, p * 1.001, p * 0.999, p, 1000.0))
    return bars


def gen_ranging(n=500, base=100.0, amp=0.03, noise=0.003, seed=7):
    """震荡行情 —— 网格策略的主战场"""
    rng = random.Random(seed)
    bars = []
    for i in range(n):
        # 多个正弦叠加，形成不规则的震荡
        wave = (math.sin(i / 17.0) * 0.5 +
                math.sin(i / 7.0) * 0.3 +
                math.sin(i / 31.0) * 0.2)
        p = base * (1.0 + wave * amp + rng.gauss(0, noise))
        bars.append(Bar(i * 900_000, p, p * 1.002, p * 0.998, p, 1000.0))
    return bars


def gen_random_walk(n=500, start=100.0, vol=0.01, seed=99):
    """几何随机游走 —— 最中性的基准"""
    rng = random.Random(seed)
    bars = []
    p = start
    for i in range(n):
        p = p * (1.0 + rng.gauss(0, vol))
        bars.append(Bar(i * 900_000, p, p * 1.002, p * 0.998, p, 1000.0))
    return bars


def gen_with_spike(n=500, base=100.0, spike_at=250, spike_mult=3.0,
                   seed=5):
    """
    含插针的行情 —— 验证价格突变保护的效果。

    如果引擎没有价格保护，插针那一根会产生灾难性成交。
    """
    bars = gen_ranging(n, base=base, amp=0.02, noise=0.002, seed=seed)
    if 0 <= spike_at < n:
        b = bars[spike_at]
        bars[spike_at] = Bar(b.ts, b.open, base * spike_mult,
                             b.low, b.close, b.volume)
    return bars


def gen_ohlcv_realistic(n=500, start=100.0, vol=0.012, drift=0.0,
                        seed=123):
    """
    更真实的 K 线：每根内部有 high/low 波动。

    这点很关键 —— 真实回测里，网格挂单是靠 high/low 触发的，
    而不是收盘价。若用收盘价判断成交，会严重低估成交次数。
    """
    rng = random.Random(seed)
    bars = []
    p = start
    for i in range(n):
        o = p
        # 日内波动
        c = o * (1.0 + rng.gauss(drift, vol))
        h = max(o, c) * (1.0 + abs(rng.gauss(0, vol * 0.5)))
        l = min(o, c) * (1.0 - abs(rng.gauss(0, vol * 0.5)))
        bars.append(Bar(i * 900_000, o, h, l, c, 1000.0))
        p = c
    return bars


# ════════════════════════════════════════════════════════════
#  真实数据载入
# ════════════════════════════════════════════════════════════

def load_csv(path, ts_col=0, o_col=1, h_col=2, l_col=3, c_col=4,
             v_col=5, has_header=True):
    """
    从 CSV 载入真实 K 线。

    交易所导出的格式通常是：
        timestamp,open,high,low,close,volume
        or
        time,open,high,low,close,volume

    若只有收盘价（部分平台），会把 high/low 设为收盘价 ——
    此时回测结果会偏乐观（低估成交次数），函数会给出警告。
    """
    import csv as _csv

    bars = []
    only_close = True
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = _csv.reader(f)
        for i, row in enumerate(reader):
            if has_header and i == 0:
                # 简单判断首行是否为表头
                try:
                    float(row[c_col])
                except (ValueError, IndexError):
                    continue
            if len(row) <= max(o_col, h_col, l_col, c_col):
                continue
            try:
                ts = float(row[ts_col]) if row[ts_col] else float(i)
                o = float(row[o_col]); h = float(row[h_col])
                l = float(row[l_col]); c = float(row[c_col])
                v = float(row[v_col]) if len(row) > v_col else 0.0
            except (ValueError, IndexError):
                continue
            if h > c * 1.0000001 or l < c * 0.9999999:
                only_close = False
            bars.append(Bar(ts, o, h, l, c, v))
    return bars, only_close


def load_ccxt_json(path):
    """
    载入 ccxt.fetch_ohlcv 保存的 JSON：
        [[ts, o, h, l, c, v], ...]
    """
    import json as _json

    with open(path, "r", encoding="utf-8") as f:
        raw = _json.load(f)
    bars = []
    for r in raw:
        if len(r) < 5:
            continue
        v = float(r[5]) if len(r) > 5 else 0.0
        bars.append(Bar(float(r[0]), r[1], r[2], r[3], r[4], v))
    return bars, False


# ════════════════════════════════════════════════════════════
#  技术指标（与实盘保持一致）
# ════════════════════════════════════════════════════════════

def sma(values, n):
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def rsi(closes, n=14):
    """Wilder RSI —— 必须与实盘 indicators.py 的算法一致"""
    if len(closes) < n + 1:
        return 50.0
    gains, losses = [], []
    for i in range(-n, 0):
        d = closes[i] - closes[i - 1]
        gains.append(max(0.0, d))
        losses.append(max(0.0, -d))
    ag = sum(gains) / n
    al = sum(losses) / n
    if al == 0:
        return 100.0 if ag > 0 else 50.0
    rs = ag / al
    return 100.0 - (100.0 / (1.0 + rs))


def bollinger(closes, n=20, k=2.0):
    if len(closes) < n:
        return (0.0, 0.0, 0.0)
    win = closes[-n:]
    m = sum(win) / n
    var = sum((x - m) ** 2 for x in win) / n
    sd = math.sqrt(var)
    return (m - k * sd, m, m + k * sd)


def atr_pct(bars, n=14):
    """ATR 占价格比例 —— 网格间距自适应依赖它"""
    if len(bars) < n + 1:
        return 0.01
    trs = []
    for i in range(-n, 0):
        h = bars[i].high; l = bars[i].low
        pc = bars[i - 1].close
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr = sum(trs) / n
    ref = bars[-1].close
    return (atr / ref) if ref > 0 else 0.01


def trend_strength(closes, n=20):
    """简单的趋势强度：线性回归斜率 / 均值"""
    if len(closes) < n:
        return 0.0
    win = closes[-n:]
    m = sum(win) / n
    if m == 0:
        return 0.0
    # 斜率
    xs = list(range(n))
    xm = sum(xs) / n
    num = sum((xs[i] - xm) * (win[i] - m) for i in range(n))
    den = sum((xs[i] - xm) ** 2 for i in range(n))
    slope = num / den if den else 0.0
    return (slope * n) / m


def build_tech(bars, i, lookback=50):
    """
    构造与实盘 _get_cached_tech 相同结构的指标字典。

    必须与 signals.py 读取的键名完全一致，否则回测结论无效。
    """
    end = i + 1
    start = max(0, end - lookback)
    seg = bars[start:end]
    closes = [b.close for b in seg]
    if len(closes) < 20:
        return None
    lo, mid, up = bollinger(closes, 20, 2.0)
    return {
        "rsi": rsi(closes, 14),
        "bb_lower": lo,
        "bb_mid": mid,
        "bb_upper": up,
        "atr_pct": atr_pct(seg, 14),
        "trend_strength": trend_strength(closes, 20),
        "close": closes[-1],
    }
