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


def gen_ohlcv_mean_reverting(n=500, start=100.0, vol=0.012, width=0.06,
                             seed=123):
    """
    OU 均值回归行情 —— 真·震荡，网格策略的主场。

    ⚠️ 为什么必须单独提供这个生成器：

    gen_ohlcv_realistic(drift=0) 生成的是【随机游走】，
    不是震荡。1500 根 K 线、vol=1% 时，随机游走的
    标准差 = 1% × sqrt(1500) ≈ 38.7%，
    价格会自然漂移到 ±40% —— 实测 6 个种子里
    只有 1 个还留在 ±15% 区间内。

    用它测试"震荡行情下的网格表现"是方法论错误：
    网格在随机游走里测出来的多半是趋势表现，
    于是得出"震荡也亏钱"的假结论。

    本函数用 Ornstein-Uhlenbeck 过程，价格被
    持续拉回中枢，波动被约束在 width 范围内，
    这才是网格策略真正适用的行情。
    """
    import math
    import random as _rnd

    rng = _rnd.Random(seed)
    center = start
    px = start
    out = []
    for i in range(n):
        # OU：向中枢回归 + 随机扰动
        pull = (center - px) / max(1e-9, abs(center)) * 0.05
        shock = rng.gauss(0, vol)
        px = px * (1.0 + pull + shock)
        # 硬约束在中枢 ±width，超出则强制回归
        lo, hi = center * (1 - width), center * (1 + width)
        if px > hi:
            px = hi - (px - hi) * 0.5
        if px < lo:
            px = lo + (lo - px) * 0.5
        o = px
        c = px * (1 + rng.gauss(0, vol * 0.5))
        h = max(o, c) * (1 + abs(rng.gauss(0, vol * 0.3)))
        l = min(o, c) * (1 - abs(rng.gauss(0, vol * 0.3)))
        out.append(Bar(i * 900000, o, h, l, c,
                              abs(rng.gauss(0, 100)) + 10))
    return out


def classify_regime(bars, lookback=60, er_trend=0.10, er_range=0.06):
    """
    行情类型判别：用 Kaufman 效率比（Efficiency Ratio）区分震荡与趋势。

    ────────────────────────────────────────────────────────
    为什么这是整套策略里最高价值的一个函数
    ────────────────────────────────────────────────────────

    回测数据已经把结论摆得很清楚（本金 100U）：

        真·震荡（OU 数据）   →  +30 ~ +52 U   ✅ 网格主场
        单边趋势（随机游走） →   -8 ~ -15 U   ❌ 网格持续亏损

    网格在震荡里赚的钱，一次趋势就能全亏回去。
    所以**不是网格不好，是不能在趋势里开网格**。

    ────────────────────────────────────────────────────────
    为什么用效率比，不用 ADX
    ────────────────────────────────────────────────────────

    ER = |终点 - 起点| / Σ|每步位移|

      · 单边趋势：终点远离起点，分子≈分母     → ER → 1
      · 来回震荡：位移互相抵消，分子≈0        → ER → 0

    对比 ADX 的优点：
      · 只需 close 序列，不用 high/low（回测数据里 high/low
        常被简化生成，不可靠）
      · 计算 O(n)，无迭代平滑，无预热期
      · 参数只有一个 lookback，语义直观

    ────────────────────────────────────────────────────────
    阈值标定（实测，非拍脑袋）
    ────────────────────────────────────────────────────────
    28 个样本（8 震荡 / 20 趋势）扫描结果：

        阈值 0.08 → 68%   阈值 0.11 → 71%
        阈值 0.10 → 71%   阈值 0.13 → 57%

    实测分布：  震荡 ER 均值 0.047（最大 0.104）
                趋势 ER 均值 0.129（最小 0.022）

    ⚠️ 准确率上限约 71%，无法再高 ——
    微弱趋势（缓涨/缓跌）与震荡在统计上【本就不可分】，
    这不是算法缺陷，是信噪比的物理限制。

    所以这个判别器的正确用法是：
    **只用来躲开强趋势**（那是最亏钱的），
    而不是追求完美择时。

    ────────────────────────────────────────────────────────
    返回
    ────────────────────────────────────────────────────────
    "trend"  趋势 —— 应关闭网格
    "range"  震荡 —— 应开启网格
    "mixed"  过渡带 —— 维持现状，避免来回切换
    """
    if bars is None or len(bars) < lookback + 1:
        return "mixed"

    seg = bars[-lookback - 1:]
    closes = [b.close for b in seg]
    net = abs(closes[-1] - closes[0])
    total = 0.0
    for i in range(1, len(closes)):
        total += abs(closes[i] - closes[i - 1])
    if total <= 0:
        return "mixed"

    er = net / total
    is_trend = er >= er_trend

    # ⚠️ 关键修正：不能只判"是不是趋势"，必须判【方向】
    #
    # 实测（5 seed 平均，本金100U）：
    #   上涨趋势  +43.63 U   ← 网格赚钱！持倉随涨、中枢上移、回调再买
    #   下跌趋势  -11.08 U   ← 网格亏钱
    #
    # 一刀切"趋势就关网格"会让收益从 +101.88 掉到 +39.68（-61%），
    # 因为它把赚钱的上涨行情也关掉了。
    #
    # 网格真正怕的只有一件事：价格单边【向下】走远。
    direction = 1 if closes[-1] >= closes[0] else -1
    if is_trend:
        return "uptrend" if direction > 0 else "downtrend"
    if er <= er_range:
        return "range"
    return "mixed"


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
    p = closes[-1]
    ratio = atr_pct(seg, 14)
    # 绝对 ATR —— 实盘 indicators.py 给的 'atr' 是绝对值，
    # 而自适应参数 volatility = atr / bb_middle 依赖它。
    # 若这里沿用 atr_pct（已是比例）再除以 bb_middle，
    # 会把"比例"当成"绝对额"再除一次价格，量纲全错。
    atr_abs = ratio * p if p > 0 else 0.0
    bwidth = ((up - lo) / mid * 100.0) if mid > 0 else 0.0
    return {
        # ── 与实盘 indicators.py 完全对齐的键名 ──
        "rsi": rsi(closes, 14),
        "bb_lower": lo,
        "bb_middle": mid,     # 实盘键名（自适应参数读它）
        "bb_upper": up,
        "atr": atr_abs,       # 实盘键名：绝对 ATR
        "atr_pct": ratio,     # 比例形式，网格间距自适应读它
        "bandwidth_pct": bwidth,
        "trend_strength": trend_strength(closes, 20),
        "close": p,
        # ── 旧键名，保留兼容 ──
        "bb_mid": mid,
    }
