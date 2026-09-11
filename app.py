# app.py — OKX 超前版策略引擎 v3.0
# 架构: 智能信号层 + 动态参数引擎 + 三层熔断 + 双模式调度
import os, time, json, hmac, base64, hashlib, threading, math
from datetime import datetime, timezone
import requests
from flask import Flask, jsonify

# ===== 环境配置 =====
TG_TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID    = os.environ.get("TELEGRAM_CHAT_ID", "")
API_KEY    = os.environ.get("OKX_API_KEY", "")
SECRET     = os.environ.get("OKX_SECRET_KEY", "")
PASSPHRASE = os.environ.get("OKX_PASSPHRASE", "")
DRY_RUN    = os.environ.get("DRY_RUN", "1") == "1"
TG, OKX = f"https://api.telegram.org/bot{TG_TOKEN}", "https://www.okx.com"
TOTAL_FUNDS = float(os.environ.get("TOTAL_FUNDS", "300"))
FEE = 0.002

def log(*a): print(datetime.now().strftime("%d日%H:%M:%S"), *a, flush=True)

# ===== Telegram 封装 =====
def tg_send(text, kb=None, edit=None):
    p = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}
    if kb: p["reply_markup"] = {"inline_keyboard": kb}
    try:
        if edit:
            p["message_id"] = edit
            requests.post(f"{TG}/editMessageText", json=p, timeout=15)
        else:
            requests.post(f"{TG}/sendMessage", json=p, timeout=15)
    except Exception as e: log("tg:", e)

def btn(t, d): return {"text": t, "callback_data": d}

# ===== OKX V5 签名请求 =====
def _sign(ts, m, path, body):
    msg = (ts + m + path + (body or "")).encode()
    return base64.b64encode(
        hmac.new(SECRET.encode(), msg, hashlib.sha256).digest()).decode()

def okx(method, path, params=None, body=None):
    if not API_KEY: return {"code": "-2", "msg": "未配置OKX API"}
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    qs = "&".join(f"{k}={v}" for k, v in (params or {}).items())
    body_s = json.dumps(body) if body else ""
    full = path + (f"?{qs}" if qs else "")
    headers = {"OK-ACCESS-KEY": API_KEY,
               "OK-ACCESS-SIGN": _sign(ts, method, full, body_s),
               "OK-ACCESS-TIMESTAMP": ts,
               "OK-ACCESS-PASSPHRASE": PASSPHRASE,
               "Content-Type": "application/json"}
    for _ in range(3):
        try:
            r = requests.request(method, OKX + full, headers=headers,
                                 data=body_s or None, timeout=15)
            return r.json()
        except Exception: time.sleep(2)
    return {"code": "-1", "msg": "网络错误"}

def place_market(inst, side, sz):
    if DRY_RUN:
        return {"code": "0", "ordId": f"DRY{int(time.time()*1000)}", "dry": True}
    r = okx("POST", "/api/v5/trade/order", body={
        "instId": inst, "tdMode": "cash", "side": side,
        "ordType": "market", "sz": str(sz)})
    if r.get("code") == "0" and r.get("data"):
        r["ordId"] = r["data"][0].get("ordId", "")
    return r

def get_price(inst):
    try:
        r = requests.get(f"{OKX}/api/v5/market/ticker?instId={inst}",
                         timeout=10).json()
        if r.get("code") == "0": return float(r["data"][0]["last"])
    except Exception: pass
    return None

# ================================================================
# 模块一: 智能信号层 (ATR/RSI/ADX 三传感器)
# ================================================================
class Indicators:
    """技术指标计算中心"""

    @staticmethod
    def get_candles(inst, bar="1D", limit=30):
        try:
            r = requests.get(f"{OKX}/api/v5/market/candles",
                params={"instId": inst, "bar": bar, "limit": limit},
                timeout=10).json()
            if r.get("code") == "0":
                return [[float(c[1]), float(c[2]), float(c[3]), float(c[4])]
                        for c in r["data"]]  # [o,h,l,c] 倒序
        except Exception: pass
        return []

    @classmethod
    def atr(cls, inst, period=14):
        """ATR: 量波动率的黄金标准"""
        cs = cls.get_candles(inst, "1D", period+1)
        if len(cs) < period+1: return None
        trs = []
        for i in range(1, len(cs)):
            h, l, pc = cs[i-1][1], cs[i-1][2], cs[i][0]
            trs.append(max(h-l, abs(h-pc), abs(l-pc)))
        return sum(trs) / len(trs)

    @classmethod
    def rsi(cls, inst, bar="1D", period=14):
        """RSI: <30超卖, >70超买"""
        cs = cls.get_candles(inst, bar, period+1)
        if len(cs) < period+1: return None
        closes = [c[3] for c in cs][::-1]
        gains, losses = [], []
        for i in range(1, len(closes)):
            d = closes[i] - closes[i-1]
            gains.append(max(d, 0)); losses.append(max(-d, 0))
        ag, al = sum(gains)/period, sum(losses)/period
        if al == 0: return 100
        return 100 - (100 / (1 + ag/al))

    @classmethod
    def adx(cls, inst, period=14):
        """ADX: >25趋势市, <20震荡区"""
        cs = cls.get_candles(inst, "1D", period*2+1)
        if len(cs) < period*2+1: return None
        # 简化ADX计算
        plus_dm, minus_dm, trs = [], [], []
        for i in range(1, len(cs)):
            h, l, pc = cs[i][1], cs[i][2], cs[i][0]
            up, dn = h - cs[i-1][1], cs[i-1][2] - l
            plus_dm.append(up if up > dn and up > 0 else 0)
            minus_dm.append(dn if dn > up and dn > 0 else 0)
            trs.append(max(h-l, abs(h-pc), abs(l-pc)))
        atr = sum(trs[-period:]) / period
        if atr == 0: return 0
        pdi = 100 * (sum(plus_dm[-period:]) / period) / atr
        mdi = 100 * (sum(minus_dm[-period:]) / period) / atr
        if pdi + mdi == 0: return 0
        dx = 100 * abs(pdi - mdi) / (pdi + mdi)
        return dx

    @classmethod
    def ma(cls, inst, period=20):
        cs = cls.get_candles(inst, "1D", period)
        if len(cs) < period: return None
        return sum(c[3] for c in cs[:period]) / period

    @classmethod
    def ewma_vol(cls, inst, period=30, lam=0.94):
        """EWMA波动率: 指数加权近30日"""
        cs = cls.get_candles(inst, "1D", period)
        if len(cs) < period: return None
        rets = []
        for i in range(1, len(cs)):
            rets.append((cs[i-1][3] - cs[i][3]) / cs[i][3])
        var, w = 0.0, 0.0
        for r in rets:
            w = 1 - math.exp(-lam)
            var = (1-w)*var + w*r*r
        return math.sqrt(var)

class MarketState:
    """市场状态检测器"""

    @staticmethod
    def detect(inst):
        """返回: RANGING / TREND_UP / TREND_DOWN / UNKNOWN"""
        adx = Indicators.adx(inst)
        ma20 = Indicators.ma(inst, 20)
        p = get_price(inst)
        if adx is None or ma20 is None or p is None:
            return "UNKNOWN"
        if adx < 20: return "RANGING"
        if adx > 25:
            return "TREND_UP" if p > ma20 else "TREND_DOWN"
        return "RANGING"

    @staticmethod
    def label(state):
        return {"RANGING": "🟡 震荡区", "TREND_UP": "📈 趋势上涨",
                "TREND_DOWN": "📉 趋势下跌", "UNKNOWN": "❓ 未知"}.get(state, "❓")

# ================================================================
# 模块二: 动态参数引擎
# ================================================================
class DynamicParams:
    """波动率自适应参数引擎"""

    # 三档参数表
    TIERS = {
        "LOW":  {"grid_atr_mult": 0.12, "dip_pct": 0.02,  "rally_pct": 0.03,  "size_coef": 1.2},
        "MID":  {"grid_atr_mult": 0.15, "dip_pct": 0.03,  "rally_pct": 0.04,  "size_coef": 1.0},
        "HIGH": {"grid_atr_mult": 0.20, "dip_pct": 0.045, "rally_pct": 0.055, "size_coef": 0.7},
    }

    @classmethod
    def get_tier(cls, inst):
        vol = Indicators.ewma_vol(inst)
        if vol is None: return "MID", 0.0
        if vol < 0.015: return "LOW", vol
        if vol > 0.035: return "HIGH", vol
        return "MID", vol

    @classmethod
    def grid_step(cls, inst):
        """ATR动态格距, 有下限保护"""
        atr = Indicators.atr(inst)
        tier, vol = cls.get_tier(inst)
        if atr is None: return 1000  # 默认值
        mult = cls.TIERS[tier]["grid_atr_mult"]
        return max(atr * mult, 500)  # 下限500防手续费侵蚀

    @classmethod
    def swing_params(cls, inst):
        tier, vol = cls.get_tier(inst)
        p = cls.TIERS[tier]
        return p["dip_pct"], p["rally_pct"], p["size_coef"]

# ================================================================
# 模块三: 三层熔断体系
# ================================================================
class RiskEngine:
    """三层风控防火墙"""

    def __init__(self):
        self.L1_price_stop = False    # 单笔浮亏 > 5%
        self.L2_float_stop = False    # 合并浮亏 > 8%
        self.L3_daily_stop = False    # 日亏 > 4%
        self.daily_pnl = 0.0
        self.daily_date = datetime.now().date()
        self.event_pause_until = 0    # 插针事件暂停

    def reset_daily(self):
        today = datetime.now().date()
        if today != self.daily_date:
            self.daily_date = today
            self.daily_pnl = 0.0
            self.L3_daily_stop = False

    def check(self, grid_float_pnl, sw_float_pnl):
        """返回 True=允许交易, False=触发熔断"""
        self.reset_daily()
        now = time.time()
        if now < self.event_pause_until: return False
        if self.L3_daily_stop: return False

        # L1: 价格级 (最灵敏)
        self.L1_price_stop = False  # 每次重置, 由单模式自行维护

        # L2: 合并浮亏
        total_float = grid_float_pnl + sw_float_pnl
        self.L2_float_stop = total_float < -TOTAL_FUNDS * 0.08
        if self.L2_float_stop and not hasattr(self, "_L2_notified"):
            self._L2_notified = True
            tg_send(f"🛡️ <b>L2熔断触发</b>\n合并浮亏 {total_float:.2f}U 超限\n双模式暂停")

        # L3: 日亏
        self.L3_daily_stop = self.daily_pnl < -TOTAL_FUNDS * 0.04
        if self.L3_daily_stop and not hasattr(self, "_L3_notified"):
            self._L3_notified = True
            tg_send(f"🛑 <b>L3日亏熔断</b>\n当日亏损 {self.daily_pnl:.2f}U 超限\n全停机至明日")

        if not self.L2_float_stop: self._L2_notified = None
        if not self.L3_daily_stop: self._L3_notified = None
        return not (self.L2_float_stop or self.L3_daily_stop)

    def on_trade(self, pnl):
        self.daily_pnl += pnl

    def on_spike(self, pct_change):
        """插针事件: 1分钟波动>3%暂停30分钟"""
        if abs(pct_change) > 0.03:
            self.event_pause_until = time.time() + 1800
            tg_send(f"⚡ <b>插针事件</b>\n1分钟波动 {pct_change*100:.1f}%\n暂停30分钟")

RISK = RiskEngine()

# ================================================================
# 模块四: 网格模式 (超前版)
# ================================================================
GRID = {
    "instId": "BTC-USDT",
    "lo": None, "hi": None,
    "n": 10, "sz": 0.0001, "max_inv": 10,
    "running": False,
    "step": None, "last_iv": None,
    "inv": {},  # {格线号: 数量}
    "rounds": 0, "gross": 0.0, "fees": 0.0, "recs": [],
    "rounds_7d": 0, "cap": TOTAL_FUNDS * 0.6,
    "last_rebase": 0,
}

def _iv(p):
    return max(0, min(GRID["n"], int((p - GRID["lo"]) / GRID["step"])))

def grid_rebase():
    """动态重算格距 + 几何分布"""
    GRID["step"] = DynamicParams.grid_step(GRID["instId"])
    GRID["inv"], GRID["last_iv"] = {}, None

def grid_shift_range():
    """移动区间: 价格漂移出界后整体平移"""
    if not GRID["lo"] or not GRID["hi"]: return
    p = get_price(GRID["instId"])
    if p is None: return
    range_w = GRID["hi"] - GRID["lo"]
    if p > GRID["hi"] + range_w * 0.1:  # 价格超上限10%
        new_center = p
        GRID["lo"] = round(new_center - range_w/2)
        GRID["hi"] = round(new_center + range_w/2)
        grid_rebase()
        tg_send(f"📐 网格区间上移: {GRID['lo']:,}~{GRID['hi']:,}")
    elif p < GRID["lo"] - range_w * 0.1:
        new_center = p
        GRID["lo"] = round(new_center - range_w/2)
        GRID["hi"] = round(new_center + range_w/2)
        grid_rebase()
        tg_send(f"📐 网格区间下移: {GRID['lo']:,}~{GRID['hi']:,}")

def grid_start():
    if not GRID["lo"] or not GRID["hi"]:
        tg_send("⚠️ 请先设置价格区间")
        return False
    grid_rebase()
    p = get_price(GRID["instId"])
    if p is None: return False
    GRID["last_iv"] = _iv(p)
    GRID["running"] = True
    tier, vol = DynamicParams.get_tier(GRID["instId"])
    step = DynamicParams.grid_step(GRID["instId"])
    tg_send(f"✅ <b>网格启动(超前版)</b>\n"
            f"区间: {GRID['lo']:,}~{GRID['hi']:,}\n"
            f"ATR动态格距: {step:.0f}\n"
            f"波动率档位: {tier} (vol={vol:.3f})\n"
            f"市场状态: {MarketState.label(MarketState.detect(GRID['instId']))}")
    return True

def grid_tick():
    if not GRID["running"] or GRID["step"] is None: return

    # 市场状态过滤
    state = MarketState.detect(GRID["instId"])
    if state == "TREND_DOWN":
        # 单边下跌: 只卖不买
        pass  # 买入逻辑跳过
    elif state == "TREND_UP":
        pass  # 卖出逻辑跳过

    # 区间漂移检查
    if time.time() - GRID["last_rebase"] > 3600:  # 每小时一次
        grid_shift_range()
        GRID["last_rebase"] = time.time()

    # 动态格距更新 (每10回合或24小时)
    if GRID["rounds"] % 10 == 0 and GRID["rounds"] > 0:
        new_step = DynamicParams.grid_step(GRID["instId"])
        if new_step != GRID["step"]:
            GRID["step"] = new_step
            tg_send(f"📊 ATR格距更新: {GRID['step']:.0f}")

    p = get_price(GRID["instId"])
    if p is None: return
    if GRID["last_iv"] is None:
        GRID["last_iv"] = _iv(p); return

    cur, last = _iv(p), GRID["last_iv"]
    if cur == last: return

    grid_float = sum((p - GRID["lo"] - k*GRID["step"]) * v
                     for k, v in GRID["inv"].items()) * GRID["sz"]
    if not RISK.check(grid_float, SW.get("float_pnl", 0)): return

    # 跌穿格线 → 买入 (趋势下跌时跳过)
    if cur < last and state != "TREND_DOWN":
        line = last
        if GRID["inv"].get(line, 0) == 0 and sum(GRID["inv"].values()) < GRID["max_inv"]:
            usdt_cost = GRID["sz"] * p
            if usdt_cost <= GRID["cap"]:
                _g_trade("buy", line, p)
    # 升穿格线 → 卖出 (趋势上涨时也卖, 但可保留底仓)
    elif cur > last:
        below = last
        if GRID["inv"].get(below, 0) > 0:
            _g_trade("sell", below, p)

    GRID["last_iv"] = cur

def _g_trade(side, line, px):
    r = place_market(GRID["instId"], side, GRID["sz"])
    if r.get("code") != "0": return False
    fee = px * GRID["sz"] * FEE
    GRID["fees"] += fee
    GRID["recs"].append({"t": time.time(), "side": side, "px": px, "sz": GRID["sz"]})
    if side == "buy":
        GRID["inv"][line] = GRID["inv"].get(line, 0) + 1
        tg_send(f"🟢 网格买 第{line}格 @{px:,.0f}")
    else:
        GRID["inv"][line] = GRID["inv"].get(line, 0) - 1
        GRID["rounds"] += 1
        GRID["rounds_7d"] += 1
        pnl = GRID["step"] * GRID["sz"] - fee
        RISK.on_trade(pnl)
        tg_send(f"🔴 网格卖 回合{GRID['rounds']} @{px:,.0f} 净{pnl:.2f}U")
    return True

# ================================================================
# 模块四B: 低吸高卖 (超前版)
# ================================================================
SW = {
    "instId": "BTC-USDT",
    "base": None, "running": False,
    "sz_base": 0.0001, "max_pos": 0.001,
    "cooldown": 600,
    "pos": 0.0, "last_ts": 0,
    # 金字塔分层
    "layers": [],  # [(数量, 入场价)]
    "layer_idx": 0,
    # 移动止损
    "trail_stop": None, "trail_pct": 0.05,
    "entry_avg": None,
    "recs": [], "pnl_7d": 0.0, "float_pnl": 0.0,
    "cap": TOTAL_FUNDS * 0.4,
}

PYRAMID_LAYERS = [
    {"trigger": 0.03, "mult": 1.0},   # 跌3%: 1.0×
    {"trigger": 0.05, "mult": 1.5},   # 跌5%: 1.5×
    {"trigger": 0.075, "mult": 2.3},  # 跌7.5%: 2.3×
    {"trigger": 0.10, "mult": 3.4},   # 跌10%: 3.4× (硬顶)
]

def swing_tick():
    if not SW["running"]: return
    p = get_price(SW["instId"])
    if p is None: return
    if SW["base"] is None:
        SW["base"] = p; return

    # 市场状态过滤
    state = MarketState.detect(SW["instId"])
    if state == "TREND_DOWN":
        SW["float_pnl"] = (p - SW["entry_avg"]) * SW["pos"] if SW["entry_avg"] else 0
        if not RISK.check(0, SW["float_pnl"]): return

    # RSI双重确认 (日线+4小时)
    rsi_d = Indicators.rsi(SW["instId"], "1D", 14)
    rsi_4h = Indicators.rsi(SW["instId"], "4H", 14)
    if rsi_d is None: return

    # 动态阈值
    dip_pct, rally_pct, size_coef = DynamicParams.swing_params(SW["instId"])
    dev = (p - SW["base"]) / SW["base"]
    now = time.time()

    # ===== 移动止损检查 =====
    if SW["pos"] > 0 and SW["trail_stop"]:
        if p <= SW["trail_stop"]:
            _sw_sell_all(p, reason="移动止损")
            return
        elif p > SW["entry_avg"]:
            new_stop = p * (1 - SW["trail_pct"])
            if new_stop > SW["trail_stop"]:
                SW["trail_stop"] = new_stop

    # ===== 金字塔买入 =====
    if now - SW["last_ts"] < SW["cooldown"]: return

    if dev <= -dip_pct and SW["layer_idx"] < 4:
        # RSI确认: 日线<30 且 4小时开始回升
        if rsi_d < 30 and (rsi_4h is None or rsi_4h > 40):
            layer = PYRAMID_LAYERS[SW["layer_idx"]]
            sz = SW["sz_base"] * layer["mult"] * size_coef
            if SW["pos"] + sz <= SW["max_pos"] and sz * p <= SW["cap"]:
                _sw_buy_layer(p, sz, layer["trigger"])
        elif rsi_d < 30 and rsi_4h and rsi_4h < 35:
            log(f"⏸️ 等待4小时RSI回升 (日:{rsi_d:.0f} 4h:{rsi_4h:.0f})")

    # ===== 卖出 (全平) =====
    elif dev >= rally_pct and SW["pos"] > 0:
        _sw_sell_all(p, reason="涨幅达标")

def _sw_buy_layer(p, sz, trigger):
    r = place_market(SW["instId"], "buy", sz)
    if r.get("code") != "0": return
    SW["layers"].append((sz, p))
    SW["pos"] = round(SW["pos"] + sz, 6)
    SW["layer_idx"] += 1
    SW["last_ts"] = time.time()
    # 更新均价和止损线
    total_cost = sum(s*px for s, px in SW["layers"])
    SW["entry_avg"] = total_cost / SW["pos"]
    SW["trail_stop"] = SW["entry_avg"] * (1 - SW["trail_pct"])
    SW["base"] = p  # 重置基准
    SW["recs"].append({"t": time.time(), "side": "buy", "px": p, "sz": sz})
    tg_send(f"🟢 <b>金字塔第{SW['layer_idx']}层</b>\n"
            f"{sz} BTC @{p:,.0f} (×{PYRAMID_LAYERS[SW['layer_idx']-1]['mult']})\n"
            f"累计仓 {SW['pos']:.4f} 均价 {SW['entry_avg']:,.0f}\n"
            f"止损线 {SW['trail_stop']:,.0f}")

def _sw_sell_all(p, reason):
    if SW["pos"] <= 0: return
    r = place_market(SW["instId"], "sell", SW["pos"])
    if r.get("code") != "0": return
    pnl = (p - SW["entry_avg"]) * SW["pos"] - p * SW["pos"] * FEE
    RISK.on_trade(pnl)
    SW["pnl_7d"] += pnl
    SW["recs"].append({"t": time.time(), "side": "sell", "px": p, "sz": SW["pos"]})
    tg_send(f"🔴 <b>全平</b> [{reason}]\n"
            f"{SW['pos']:.4f} BTC @{p:,.0f}\n"
            f"回合净利 {pnl:+.2f}U")
    # 重置
    SW["pos"] = 0; SW["layers"] = []; SW["layer_idx"] = 0
    SW["entry_avg"] = None; SW["trail_stop"] = None
    SW["base"] = p; SW["last_ts"] = time.time()

# ================================================================
# 模块五: 资金调度引擎
# ================================================================
def rebalance_funds():
    """每24小时动态再平衡"""
    grid_profit = GRID["rounds_7d"] * GRID["step"] * GRID["sz"]
    swing_profit = SW["pnl_7d"]
    total = grid_profit + swing_profit
    if total > 0:
        grid_share = grid_profit / total
        GRID["cap"] = TOTAL_FUNDS * max(0.4, min(0.7, grid_share))
        SW["cap"] = TOTAL_FUNDS - GRID["cap"]
    # 高波动降仓
    vol = Indicators.ewma_vol("BTC-USDT")
    if vol and vol > 0.035:
        GRID["cap"] *= 0.7; SW["cap"] *= 0.7

# ================================================================
# Telegram 页面
# ================================================================
def page_home():
    p = get_price("BTC-USDT")
    state = MarketState.detect("BTC-USDT")
    tier, vol = DynamicParams.get_tier("BTC-USDT")
    grid_float = sum((p - GRID["lo"] - k*GRID["step"]) * v
                     for k, v in GRID["inv"].items()) * GRID["sz"] if p and GRID["step"] else 0
    t = (f"🤖 <b>OKX 超前版策略引擎</b> 🟢\n\n"
         f"📊 BTC: {p:,.0f}\n"
         f"👁️ 市场状态: {MarketState.label(state)}\n"
         f"📉 波动率: {tier}档 ({vol:.3f})\n\n"
         f"📐 网格: {'▶️' if GRID['running'] else '⏸️'} "
         f"回合{GRID['rounds']} 净利{GRID['gross']-GRID['fees']:+.2f}U\n"
         f"📉 低吸: {'▶️' if SW['running'] else '⏸️'} "
         f"仓{SW['pos']:.4f} 浮盈{SW['float_pnl']:+.2f}U\n\n"
         f"🛡️ 熔断: L1{'🔴' if RISK.L1_price_stop else '🟢'} "
         f"L2{'🔴' if RISK.L2_float_stop else '🟢'} "
         f"L3{'🔴' if RISK.L3_daily_stop else '🟢'}\n"
         f"💰 日盈亏: {RISK.daily_pnl:+.2f}U\n\n"
         f"🧪 {'模拟' if DRY_RUN else '⚠️实盘'}")
    kb = [[btn("📐 网格(超前)", "grid"), btn("📉 低吸(超前)", "dip")],
          [btn("🛡️ 风控面板", "risk"), btn("🔄 刷新", "home")]]
    return t, kb

def page_grid():
    p = get_price(GRID["instId"])
    state = MarketState.detect(GRID["instId"])
    step = DynamicParams.grid_step(GRID["instId"])
    atr = Indicators.atr(GRID["instId"])
    rsi = Indicators.rsi(GRID["instId"])
    adx = Indicators.adx(GRID["instId"])
    t = (f"📐 <b>网格(超前版)</b> {'▶️' if GRID['running'] else '⏸️'}\n\n"
         f"区间: {GRID['lo'] or '未设'}~{GRID['hi'] or '未设'}\n"
         f"ATR动态格距: {step:.0f} (ATR={atr:.0f})\n"
         f"状态: {MarketState.label(state)}\n"
         f"ADX={adx:.0f} RSI={rsi:.0f}\n\n"
         f"回合: {GRID['rounds']} 净利: {GRID['gross']-GRID['fees']:+.2f}U\n"
         f"持货: {sum(GRID['inv'].values())}格/{GRID['max_inv']}\n\n"
         f"风控: 趋势下跌只卖不买\n"
         f"     ATR每10回合自动调格距\n"
         f"     区间漂移10%自动平移")
    kb = [[btn("⏸️ 暂停" if GRID["running"] else "▶️ 启动", "g_tog")],
          [btn("⚡区间±10%", "g_r10"), btn("⚙️ 参数", "g_par")],
          [btn("⬅️ 主页", "home")]]
    return t, kb

def page_dip():
    p = get_price(SW["instId"])
    rsi_d = Indicators.rsi(SW["instId"], "1D")
    rsi_4h = Indicators.rsi(SW["instId"], "4H")
    dip, rally, coef = DynamicParams.swing_params(SW["instId"])
    t = (f"📉 <b>低吸(超前版)</b> {'▶️' if SW['running'] else '⏸️'}\n\n"
         f"当前: {p:,.0f} 基准: {SW['base'] or '未设'}\n"
         f"动态阈值: 跌{dip*100:.1f}%买 涨{rally*100:.1f}%卖\n\n"
         f"RSI确认: 日{rsi_d:.0f} 4h{rsi_4h:.0f}\n"
         f"金字塔: 第{SW['layer_idx']}层/4\n"
         f"仓位: {SW['pos']:.4f}/{SW['max_pos']:.4f}\n"
         f"均价: {SW['entry_avg'] or '—'}\n"
         f"止损线: {SW['trail_stop'] or '—'}\n\n"
         f"⚠️ 层级: 3%→1× 5%→1.5×\n"
         f"     7.5%→2.3× 10%→3.4×(顶)")
    kb = [[btn("⏸️ 暂停" if SW["running"] else "▶️ 启动", "d_tog")],
          [btn("⬅️ 主页", "home")]]
    return t, kb

def page_risk():
    t = (f"🛡️ <b>三层熔断面板</b>\n\n"
         f"L1 价格级: {'🔴触发' if RISK.L1_price_stop else '🟢正常'}\n"
         f"L2 浮亏级: {'🔴触发' if RISK.L2_float_stop else '🟢正常'}\n"
         f"L3 日亏级: {'🔴触发' if RISK.L3_daily_stop else '🟢正常'}\n\n"
         f"今日盈亏: {RISK.daily_pnl:+.2f}U\n"
         f"网格资金: {GRID['cap']:.0f}U\n"
         f"低吸资金: {SW['cap']:.0f}U\n\n"
         f"插针暂停: {'是' if time.time()<RISK.event_pause_until else '否'}")
    kb = [[btn("🔄 解除L2", "rk_l2"), btn("🔄 解除L3", "rk_l3")],
          [btn("⬅️ 主页", "home")]]
    return t, kb

PAGES = {"home": page_home, "grid": page_grid, "dip": page_dip, "risk": page_risk}

def on_cb(data, mid):
    if data in PAGES:
        t, kb = PAGES[data](); tg_send(t, kb, edit=mid)
    elif data == "g_tog":
        if GRID["running"]:
            GRID["running"] = False
        else:
            grid_start()
        t, kb = page_grid(); tg_send(t, kb, edit=mid)
    elif data == "g_r10":
        p = get_price(GRID["instId"])
        if p:
            GRID["running"] = False
            GRID["lo"], GRID["hi"] = round(p*0.9), round(p*1.1)
            grid_rebase()
        t, kb = page_grid(); tg_send(t, kb, edit=mid)
    elif data == "d_tog":
        SW["running"] = not SW["running"]
        if SW["running"] and SW["base"] is None:
            p = get_price(SW["instId"])
            if p: SW["base"] = p
        t, kb = page_dip(); tg_send(t, kb, edit=mid)
    elif data == "rk_l2":
        RISK.L2_float_stop = False
        t, kb = page_risk(); tg_send(t, kb, edit=mid)
    elif data == "rk_l3":
        RISK.L3_daily_stop = False
        t, kb = page_risk(); tg_send(t, kb, edit=mid)

def on_text(txt):
    if txt in ("/menu", "/start"):
        t, kb = page_home(); tg_send(t, kb); return
    tg_send("💡 发送 /menu")

# ================================================================
# 主循环 + Flask
# ================================================================
_last_price_for_spike = None
def engine_loop():
    global _last_price_for_spike
    while True:
        try:
            p = get_price("BTC-USDT")
            if p:
                if _last_price_for_spike:
                    pct = (p - _last_price_for_spike) / _last_price_for_spike
                    RISK.on_spike(pct)  # 插针检测
                _last_price_for_spike = p
            grid_tick()
            swing_tick()
            # 每24小时资金再平衡
            if datetime.now().hour == 0 and not hasattr(engine_loop, "_rb"):
                rebalance_funds(); engine_loop._rb = True
            elif datetime.now().hour != 0:
                engine_loop._rb = None
        except Exception as e:
            log("engine:", e)
        time.sleep(10)

def tg_poll_loop():
    offset = 0
    while True:
        try:
            r = requests.get(f"{TG}/getUpdates",
                params={"timeout": 25, "offset": offset + 1}, timeout=30).json()
            for u in r.get("result", []):
                offset = u["update_id"]
                if "callback_query" in u:
                    cq = u["callback_query"]
                    if str(cq["message"]["chat"]["id"]) == CHAT_ID:
                        on_cb(cq["data"], cq["message"]["message_id"])
                    try:
                        requests.post(f"{TG}/answerCallbackQuery",
                            json={"callback_query_id": cq["id"]}, timeout=10)
                    except Exception: pass
                elif "message" in u:
                    m = u["message"]
                    if str(m.get("chat", {}).get("id")) == CHAT_ID and m.get("text"):
                        on_text(m["text"])
        except Exception: time.sleep(3)

app = Flask(__name__)

@app.route("/")
@app.route("/health")
def health():
    return jsonify({"status": "ok", "dry_run": DRY_RUN,
        "market": MarketState.detect("BTC-USDT"),
        "tier": DynamicParams.get_tier("BTC-USDT")[0],
        "risk": {"L1": RISK.L1_price_stop, "L2": RISK.L2_float_stop,
                 "L3": RISK.L3_daily_stop},
        "grid": {"running": GRID["running"], "rounds": GRID["rounds"]},
        "swing": {"running": SW["running"], "pos": SW["pos"]}})

if __name__ == "__main__":
    tg_send("🤖 <b>超前版策略引擎已启动</b>\n"
            f"架构: 三指标信号 + 动态参数 + 三层熔断\n"
            f"模式: {'🧪模拟' if DRY_RUN else '⚠️实盘'}\n"
            "发送 /menu")
    threading.Thread(target=engine_loop, daemon=True).start()
    threading.Thread(target=tg_poll_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
