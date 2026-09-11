# app.py — OKX 多币种策略引擎 v3.6 完整版
# ============================================================
# 数据铁律 (全链路无任何假设值):
#   1. 余额/价格/K线/指标/盘口/费率/成交记录 → 一律OKX实时API
#   2. 网格每格记录真实买入成交价; 回合盈亏 = 真实卖价−真实买价−双边真实手续费
#   3. 模拟模式成交价 = 真实盘口一档价(卖吃bid/买吃ask); 手续费 = 真实费率
#   4. 重启重建: 持仓/均价/每格买价/日盈亏 全部从交易所成交记录回放
#   5. 日切不清零 → 重新查询当日真实成交
#   6. DRY_RUN唯一作用: 拦截发往交易所的订单
# ============================================================
import os, time, json, math, hmac, base64, hashlib, threading, requests
from datetime import datetime, timezone
from flask import Flask, jsonify

TG_TOKEN  = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
API_KEY   = os.environ.get("OKX_API_KEY", "")
SECRET    = os.environ.get("OKX_SECRET_KEY", "")
PASSPHR   = os.environ.get("OKX_PASSPHRASE", "")
DRY_RUN   = os.environ.get("DRY_RUN", "1") == "1"
COIN_PATH = os.environ.get("STATE_PATH", "/tmp/coins.json")   # 仅存币种列表(用户配置)
TG  = f"https://api.telegram.org/bot{TG_TOKEN}"
OKX = "https://www.okx.com"

def log(*a):
    print(datetime.now().strftime("%d日%H:%M:%S"), *a, flush=True)

# ============================================================
# Telegram (限流合并)
# ============================================================
TG_LOCK = threading.Lock()
TG_SENT_TS, TG_PENDING = [], []

def tg_send(text, kb=None, edit=None):
    if edit:
        try:
            p = {"chat_id": CHAT_ID, "message_id": edit, "text": text,
                 "parse_mode": "HTML"}
            if kb: p["reply_markup"] = {"inline_keyboard": kb}
            requests.post(f"{TG}/editMessageText", json=p, timeout=15)
        except Exception as e: log("tg edit:", e)
        return
    now = time.time()
    with TG_LOCK:
        TG_SENT_TS[:] = [t for t in TG_SENT_TS if now - t < 60]
        if len(TG_SENT_TS) >= 18:
            TG_PENDING.append(text)
            if len(TG_PENDING) >= 5:
                merged = "📨 <b>高频消息摘要</b>\n" + "\n".join(TG_PENDING[-5:])
                TG_PENDING.clear()
            else:
                return
        else:
            TG_SENT_TS.append(now)
            merged = text
    p = {"chat_id": CHAT_ID, "text": merged, "parse_mode": "HTML"}
    if kb: p["reply_markup"] = {"inline_keyboard": kb}
    try: requests.post(f"{TG}/sendMessage", json=p, timeout=15)
    except Exception as e: log("tg:", e)

def btn(t, d): return {"text": t, "callback_data": d}

# ============================================================
# OKX V5 核心
# ============================================================
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
               "OK-ACCESS-PASSPHRASE": PASSPHR,
               "Content-Type": "application/json"}
    for _ in range(3):
        try:
            r = requests.request(method, OKX + full, headers=headers,
                                 data=body_s or None, timeout=15)
            j = r.json()
            if j.get("code") != "0" and "SELF" in globals():
                SELF.api_fail(path)
            return j
        except Exception:
            time.sleep(2)
    return {"code": "-1", "msg": "网络错误"}

def place_market(inst, side, sz):
    """DRY_RUN唯一作用点: 模拟→不发真实订单"""
    if DRY_RUN:
        return {"code": "0", "ordId": f"DRY{int(time.time()*1000)}"}
    body = {"instId": inst, "tdMode": "cash", "side": side,
            "ordType": "market", "sz": str(sz)}
    if side == "buy":
        body["tgtCcy"] = "base_ccy"
    r = okx("POST", "/api/v5/trade/order", body=body)
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

def get_book_px(inst, side):
    """真实盘口一档价: 卖吃bid / 买吃ask — 模拟模式成交价来源"""
    try:
        r = requests.get(f"{OKX}/api/v5/market/books",
            params={"instId": inst, "sz": "1"}, timeout=10).json()
        if r.get("code") == "0" and r.get("data"):
            b = r["data"][0]
            return (float(b["bids"][0][0]) if side == "sell"
                    else float(b["asks"][0][0]))
    except Exception: pass
    return None

def fetch_instrument(inst):
    try:
        r = requests.get(f"{OKX}/api/v5/public/instruments",
            params={"instType": "SPOT", "instId": inst}, timeout=10).json()
        if r.get("code") == "0":
            d = r["data"][0]
            return float(d["minSz"]), float(d["lotSz"])
    except Exception: pass
    return None, None

# ============================================================
# 指标层 + 缓存
# ============================================================
class Indicators:
    @staticmethod
    def get_candles(inst, bar="1D", limit=30):
        try:
            r = requests.get(f"{OKX}/api/v5/market/candles",
                params={"instId": inst, "bar": bar, "limit": limit},
                timeout=10).json()
            if r.get("code") == "0":
                return [[float(c[1]), float(c[2]), float(c[3]), float(c[4])]
                        for c in r["data"]]
        except Exception: pass
        return []

    @classmethod
    def atr(cls, inst, bar="1D", period=14):
        cs = cls.get_candles(inst, bar, period + 1)
        if len(cs) < period + 1: return None
        trs = []
        for i in range(1, len(cs)):
            h, l, pc = cs[i-1][1], cs[i-1][2], cs[i][0]
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        return sum(trs) / len(trs)

    @classmethod
    def rsi(cls, inst, bar="1D", period=14):
        cs = cls.get_candles(inst, bar, period + 1)
        if len(cs) < period + 1: return None
        closes = [c[3] for c in cs][::-1]
        gains, losses = [], []
        for i in range(1, len(closes)):
            d = closes[i] - closes[i-1]
            gains.append(max(d, 0)); losses.append(max(-d, 0))
        ag, al = sum(gains)/period, sum(losses)/period
        if al == 0: return 100
        return 100 - 100 / (1 + ag/al)

    @classmethod
    def adx(cls, inst, bar="1D", period=14):
        cs = cls.get_candles(inst, bar, period*2 + 1)
        if len(cs) < period*2 + 1: return None
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
        return 100 * abs(pdi - mdi) / (pdi + mdi)

    @classmethod
    def ma(cls, inst, bar="1D", period=20):
        cs = cls.get_candles(inst, bar, period)
        if len(cs) < period: return None
        return sum(c[3] for c in cs[:period]) / period

    @classmethod
    def ewma_vol(cls, inst, bar="1D", period=30):
        cs = cls.get_candles(inst, bar, period)
        if len(cs) < period: return None
        rets = [(cs[i-1][3] - cs[i][3]) / cs[i][3] for i in range(1, len(cs))]
        var, lam, w = 0.0, 0.94, 0.06
        for r in rets:
            var = lam * var + w * r * r
        return math.sqrt(var)

class IndicatorCache:
    TTL = 300
    _c = {}
    @classmethod
    def get(cls, fn, inst, bar="1D", *args):
        key = (inst, fn.__name__, bar, args)
        hit = cls._c.get(key)
        if hit and time.time() - hit[1] < cls.TTL:
            return hit[0]
        v = fn(inst, bar, *args)
        cls._c[key] = (v, time.time())
        return v

class MarketState:
    @staticmethod
    def detect(inst):
        adx  = IndicatorCache.get(Indicators.adx, inst, "1D", 14)
        ma20 = IndicatorCache.get(Indicators.ma, inst, "1D", 20)
        p = get_price(inst)
        if adx is None or ma20 is None or p is None: return "UNKNOWN"
        if adx < 20: return "RANGING"
        if adx > 25: return "TREND_UP" if p > ma20 else "TREND_DOWN"
        return "RANGING"

    @staticmethod
    def label(s):
        return {"RANGING": "🟡震荡", "TREND_UP": "📈趋势涨",
                "TREND_DOWN": "📉趋势跌", "UNKNOWN": "❓未知"}.get(s, "❓")

class DynamicParams:
    TIERS = {
        "LOW":  {"grid_mult": 0.12, "dip": 0.02,  "rally": 0.03,  "coef": 1.2},
        "MID":  {"grid_mult": 0.15, "dip": 0.03,  "rally": 0.04,  "coef": 1.0},
        "HIGH": {"grid_mult": 0.20, "dip": 0.045, "rally": 0.055, "coef": 0.7},
    }
    @classmethod
    def tier(cls, inst):
        vol = IndicatorCache.get(Indicators.ewma_vol, inst, "1D", 30)
        if vol is None: return "MID", 0.0
        if vol < 0.015: return "LOW", vol
        if vol > 0.035: return "HIGH", vol
        return "MID", vol

    @classmethod
    def grid_step(cls, inst):
        atr = IndicatorCache.get(Indicators.atr, inst, "1D", 14)
        t, _ = cls.tier(inst)
        if atr is None: return None
        return max(atr * cls.TIERS[t]["grid_mult"], 500)

    @classmethod
    def swing_params(cls, inst):
        t, _ = cls.tier(inst)
        p = cls.TIERS[t]
        return p["dip"], p["rally"], p["coef"]

# ============================================================
# 实时余额 — 资金唯一真源 (任何模式)
# ============================================================
class LiveBalance:
    TTL = 30
    def __init__(self):
        self._cache, self._ts, self.last_err = None, 0, None

    def get(self, force=False):
        now = time.time()
        if not force and self._cache and now - self._ts < self.TTL:
            return self._cache
        r = okx("GET", "/api/v5/account/balance", params={"ccy": "USDT"})
        if r.get("code") == "0" and r.get("data"):
            for d in (r["data"][0].get("details") or []):
                if d.get("ccy") == "USDT":
                    self._cache = {"avail":  float(d.get("availEq") or 0),
                                   "eq":     float(d.get("eq") or 0),
                                   "frozen": float(d.get("frozenBal") or 0)}
                    self._ts, self.last_err = now, None
                    return self._cache
        self.last_err = r.get("msg", "unknown")
        return None

    def get_funding(self):
        r = okx("GET", "/api/v5/asset/balances", params={"ccy": "USDT"})
        if r.get("code") == "0" and r.get("data"):
            return float(r["data"][0].get("availBal") or 0)
        return None

    def transfer_to_trading(self, amt):
        return okx("POST", "/api/v5/asset/transfer",
                   body={"ccy": "USDT", "amt": f"{amt:.2f}",
                         "from": "6", "to": "18"})

LIVE = LiveBalance()

def get_funds():
    """任何模式都查真实API, 无虚拟分支. 失败→None触发Fail-Safe"""
    bal = LIVE.get()
    return (bal, "🟢实时") if bal else (None, "⚠️API失败")

# ============================================================
# 真实费率 (从你的历史成交推导)
# ============================================================
class FeeProfile:
    _rate, _ts = None, 0
    @classmethod
    def rate(cls):
        if cls._rate and time.time() - cls._ts < 86400:
            return cls._rate
        try:
            r = okx("GET", "/api/v5/trade/fills-history",
                    params={"instType": "SPOT", "limit": "50"})
            notional, fees = 0.0, 0.0
            if r.get("code") == "0":
                for f in r.get("data", []):
                    notional += float(f["fillPx"]) * float(f["fillSz"])
                    fees += abs(float(f["fee"]))
            if notional > 0:
                cls._rate, cls._ts = fees / notional, time.time()
                return cls._rate
        except Exception: pass
        return None   # 无历史成交→诚实返回None

# ============================================================
# 真实成交引擎 (回查/分页/持仓与买价重建)
# ============================================================
class FillEngine:
    @staticmethod
    def get_fill(ord_id, inst):
        """实盘: 回查订单真实成交均价与实扣手续费"""
        time.sleep(1)
        r = okx("GET", "/api/v5/trade/fills",
                params={"instId": inst, "ordId": ord_id})
        if r.get("code") != "0" or not r.get("data"): return None
        fills = r["data"]
        total_sz = sum(float(f["fillSz"]) for f in fills)
        if total_sz == 0: return None
        avg_px = sum(float(f["fillPx"]) * float(f["fillSz"]) for f in fills) / total_sz
        fee = sum(abs(float(f["fee"])) for f in fills)
        return {"px": avg_px, "sz": total_sz, "fee": fee}

    @staticmethod
    def fills_paged(inst, pages=3, limit=100):
        """分页拉取成交记录(3页≈300笔), 时间倒序去重 — 用于重建每格真实买价"""
        out, seen = [], set()
        for ep in ("/api/v5/trade/fills", "/api/v5/trade/fills-history"):
            after = None
            for _ in range(pages):
                params = {"instType": "SPOT", "instId": inst, "limit": str(limit)}
                if after: params["after"] = after
                r = okx("GET", ep, params=params)
                if r.get("code") != "0": break
                rows = r.get("data", [])
                if not rows: break
                for f in rows:
                    if f["billId"] not in seen:
                        seen.add(f["billId"])
                        out.append(f)
                after = rows[-1]["billId"]
        out.sort(key=lambda f: int(f["ts"]), reverse=True)
        return out

class PositionAudit:
    @staticmethod
    def real_ccy(ccy):
        r = okx("GET", "/api/v5/account/balance", params={"ccy": ccy})
        if r.get("code") == "0" and r.get("data"):
            for d in (r["data"][0].get("details") or []):
                if d.get("ccy") == ccy:
                    return (float(d.get("availEq") or 0)
                            + float(d.get("frozenBal") or 0))
        return None

    @staticmethod
    def audit(st):
        real = PositionAudit.real_ccy(st.inst.split("-")[0])
        if real is None or not st.g["sz"]: return None, real
        mem = st.s["pos"] + sum(d["qty"] for d in st.g["inv"].values()) * st.g["sz"]
        return round(real - mem, 8), real

class RebuildEngine:
    """启动重建: 持仓/均价/每格真实买价/日盈亏 全部从交易所回放"""

    @classmethod
    def rebuild_swing(cls, inst):
        ccy = inst.split("-")[0]
        real = PositionAudit.real_ccy(ccy)
        if not real or real <= 0:
            return {"pos": 0.0, "entry_avg": None}
        pos, cost, seen = 0.0, 0.0, set()
        for f in FillEngine.fills_paged(inst, pages=2):
            if f["ordId"] in seen: continue
            seen.add(f["ordId"])
            sz = float(f["fillSz"])
            if f["side"] == "buy":
                pos += sz
                cost += sz * float(f["fillPx"])
                if pos >= real * 0.999: break
        avg = round(cost / pos, 2) if pos >= real * 0.999 and pos > 0 else None
        return {"pos": real, "entry_avg": avg}

    @classmethod
    def rebuild_grid(cls, st):
        """从真实成交回放每格买价: 每笔买入按最近格线归位"""
        if st.g["step"] is None:
            st.g["step"] = DynamicParams.grid_step(st.inst)
        if st.g["step"] is None or st.g["lo"] is None: return
        ccy = st.inst.split("-")[0]
        real = PositionAudit.real_ccy(ccy)
        if real is None: return
        grid_amt = real - st.s["pos"]
        units = int(grid_amt / st.g["sz"]) if st.g["sz"] > 0 else 0
        st.g["inv"] = {}
        if units <= 0: return
        # 回放买入成交, 按真实买价归位到最近格线
        filled = 0
        for f in FillEngine.fills_paged(st.inst, pages=3):
            if filled >= units: break
            if f["side"] != "buy": continue
            px = float(f["fillPx"])
            line = round((px - st.g["lo"]) / st.g["step"])
            if line < 0 or line > st.g["n"]: continue
            if line in st.g["inv"]: continue
            st.g["inv"][line] = {"qty": 1, "buy_px": px,
                                 "fee": abs(float(f["fee"]))}
            filled += 1
        if filled < units:
            tg_send(f"⚠️ {st.inst} 网格重建: {filled}/{units}格找到真实买价\n"
                    f"其余{units-filled}格成交记录已超分页深度, 请核查")
        log(f"📦 {st.inst} 网格重建: {filled}格(含真实买价)")

    @classmethod
    def refresh_daily_pnl(cls):
        """日盈亏 = 当日真实成交回放 (现金流口径: 卖出所得−买入支出)"""
        pnl = 0.0
        for inst in list(STRATS.keys()):
            for f in FillEngine.fills_paged(inst, pages=1):
                if time.time() - int(f["ts"])/1000 > 86400: break
                v = float(f["fillPx"]) * float(f["fillSz"])
                pnl += (v + abs(float(f["fee"]))) if f["side"] == "sell" else -v
        RISK.daily_pnl = round(pnl, 2)
        return RISK.daily_pnl

    @classmethod
    def full_rebuild(cls):
        report = []
        with STRATS_LOCK:
            for inst, st in STRATS.items():
                s = cls.rebuild_swing(inst)
                st.s["pos"] = s["pos"]
                st.s["entry_avg"] = s["entry_avg"]
                if s["pos"] > 0 and s["entry_avg"]:
                    st.s["trail_stop"] = s["entry_avg"] * 0.95
                cls.rebuild_grid(st)
                diff, real = PositionAudit.audit(st)
                ok_ = diff is not None and abs(diff) < 0.00001
                n_px = sum(1 for d in st.g["inv"].values() if d["buy_px"])
                report.append(f"{'✅' if ok_ else '⚠️'} {inst}: "
                              f"低吸{s['pos']} 网格{len(st.g['inv'])}格"
                              f"(真实买价{n_px}) 交易所={real}")
        pnl = cls.refresh_daily_pnl()
        tg_send("📦 <b>启动重建完成</b> (数据源: 交易所实时成交记录)\n\n"
                + ("\n".join(report) or "无币种")
                + f"\n当日真实盈亏: {pnl:+.2f}U")

class ConfigStore:
    """仅持久化币种列表(用户配置); 账本类状态一律交易所重建"""
    @staticmethod
    def save_coins():
        try: json.dump(list(STRATS.keys()), open(COIN_PATH, "w"))
        except Exception: pass

    @staticmethod
    def load_coins():
        try:
            if os.path.exists(COIN_PATH):
                for inst in json.load(open(COIN_PATH)):
                    add_symbol(inst, notify=False)
                log(f"📦 币种列表已恢复: {list(STRATS.keys())}")
        except Exception as e: log("coins load:", e)

# ============================================================
# 多币种注册表 (线程安全)
# ============================================================
STRATS = {}
STRATS_LOCK = threading.RLock()
TOTAL_AVAIL = 0.0

class Strategy:
    def __init__(self, inst):
        self.inst = inst
        self.cap = 0.0
        self.min_sz, self.lot_sz = fetch_instrument(inst)
        # inv 结构: {line: {"qty": int, "buy_px": 真实买价, "fee": 真实买侧手续费}}
        self.g = {"running": False, "lo": None, "hi": None, "n": 10,
                  "sz": None, "max_inv": 10, "step": None, "last_iv": None,
                  "inv": {}, "rounds": 0, "fees": 0.0,
                  "last_step_ts": 0, "last_px_ts": 0}
        self.s = {"running": False, "base": None, "pos": 0.0,
                  "layers": [], "layer_idx": 0, "entry_avg": None,
                  "trail_stop": None, "last_ts": 0, "pnl_7d": 0.0}

    def has_holding(self):
        return self.s["pos"] > 0 or bool(self.g["inv"])

    def autofit(self, price):
        if price <= 0: return
        if self.has_holding(): return
        sz = self.cap * 0.6 / self.g["max_inv"] / price
        if self.min_sz and sz < self.min_sz:
            sz = self.min_sz
            max_aff = int(self.cap * 0.6 / (sz * price))
            self.g["max_inv"] = max(2, min(self.g["max_inv"], max_aff))
        if self.lot_sz:
            sz = math.floor(sz / self.lot_sz) * self.lot_sz
        self.g["sz"] = round(max(sz, 0), 8)

def round_to_lot(st, amt):
    if st.lot_sz:
        return round(math.floor(amt / st.lot_sz) * st.lot_sz, 8)
    return round(amt, 8)

def add_symbol(inst, notify=True):
    inst = inst.strip().upper()
    with STRATS_LOCK:
        if inst in STRATS: return f"⚠️ {inst} 已存在"
        p = get_price(inst)
        if p is None:
            return f"❌ 查不到 {inst} 行情 (格式: ETH-USDT / SOL-USDT)"
        st = Strategy(inst)
        if st.min_sz is None:
            return f"❌ {inst} 合约信息查询失败"
        st.cap = TOTAL_AVAIL * 0.9 / (len(STRATS) + 1) if TOTAL_AVAIL else 0
        st.g["lo"], st.g["hi"] = round(p * 0.9), round(p * 1.1)
        st.autofit(p)
        STRATS[inst] = st
    ConfigStore.save_coins()
    msg = (f"✅ <b>已添加 {inst}</b>\n"
           f"区间: {st.g['lo']:,}~{st.g['hi']:,}\n"
           f"每格: {st.g['sz']} (最小{st.min_sz}) 格数上限{st.g['max_inv']}\n"
           f"发 <code>start {inst}</code> 启动")
    if notify: tg_send(msg)
    return msg

def del_symbol(inst, notify=True):
    inst = inst.strip().upper()
    with STRATS_LOCK:
        st = STRATS.get(inst)
        if not st: return f"❌ {inst} 不在列表"
        if st.has_holding():
            return f"⚠️ {inst} 有持仓, 先发 <code>close {inst}</code>"
        del STRATS[inst]
    ConfigStore.save_coins()
    if notify: tg_send(f"🗑️ {inst} 已移除")
    return "ok"

def close_symbol(inst, notify=True):
    inst = inst.strip().upper()
    with STRATS_LOCK:
        st = STRATS.get(inst)
        if not st: return f"❌ {inst} 不存在"
        msgs = []
        if st.s["pos"] > 0:
            amt = round_to_lot(st, st.s["pos"])
            if amt > 0:
                r = place_market(inst, "sell", amt)
                if r.get("code") == "0":
                    s = _settle(r, st, "sell", amt)
                    if s:
                        msgs.append(f"平低吸{amt}@{s['px']:,.2f}")
                        st.s.update({"pos": 0, "layers": [], "layer_idx": 0,
                                     "entry_avg": None, "trail_stop": None})
        units = sum(d["qty"] for d in st.g["inv"].values())
        if units > 0 and st.g["sz"]:
            amt = round_to_lot(st, units * st.g["sz"])
            if amt > 0:
                r = place_market(inst, "sell", amt)
                if r.get("code") == "0":
                    s = _settle(r, st, "sell", amt)
                    if s:
                        msgs.append(f"平网格{units}格@{s['px']:,.2f}")
                        st.g["inv"] = {}
    msg = "✅ " + " | ".join(msgs) if msgs else "ℹ️ 无持仓"
    if notify: tg_send(msg)
    return msg

def start_symbol(inst):
    with STRATS_LOCK:
        st = STRATS.get(inst.strip().upper())
        if not st: return "❌ 币种不存在, 先 add"
        if st.g["step"] is None:
            st.g["step"] = DynamicParams.grid_step(st.inst)
        p = get_price(st.inst)
        if p is None: return "❌ 行情不可得"
        if st.g["last_iv"] is None:
            st.g["last_iv"] = _iv(st, p)
        st.g["running"] = True
        st.s["running"] = True
        if st.s["base"] is None: st.s["base"] = p
        tg_send(f"▶️ {st.inst} 双模式启动 · 格距{st.g['step']:.0f} · "
                f"{MarketState.label(MarketState.detect(st.inst))}")
        return "ok"

def stop_symbol(inst):
    with STRATS_LOCK:
        st = STRATS.get(inst.strip().upper())
        if not st: return "❌ 币种不存在"
        st.g["running"] = False
        st.s["running"] = False
    tg_send(f"⏸️ {inst.upper()} 已暂停")
    return "ok"

# ============================================================
# 三层风控 (实时权益基准)
# ============================================================
class RiskEngine:
    def __init__(self):
        self.L2_float_stop = False
        self.L3_daily_stop = False
        self.daily_pnl = 0.0
        self.daily_date = datetime.now().date()
        self.event_pause_until = 0
        self.live_funds = None

    def reset_daily(self):
        """日切: 重新查询当日真实成交, 不清零装作没发生"""
        today = datetime.now().date()
        if today != self.daily_date:
            self.daily_date = today
            self.L3_daily_stop = False
            try: RebuildEngine.refresh_daily_pnl()
            except Exception: pass

    def update_funds(self, bal):
        if bal: self.live_funds = bal["eq"]

    def check(self, grid_float, sw_float):
        if time.time() < self.event_pause_until: return False
        if self.L3_daily_stop: return False
        if self.live_funds is None: return False
        self.reset_daily()
        total = grid_float + sw_float
        if total < -self.live_funds * 0.08 and not self.L2_float_stop:
            self.L2_float_stop = True
            tg_send(f"🛡️ <b>L2浮亏熔断</b> {total:.2f}U "
                    f"(实时权益{self.live_funds:.0f}U的8%)")
        if self.daily_pnl < -self.live_funds * 0.04 and not self.L3_daily_stop:
            self.L3_daily_stop = True
            tg_send(f"🛑 <b>L3日亏熔断</b> {self.daily_pnl:.2f}U, 停机至明日")
        return not (self.L2_float_stop or self.L3_daily_stop)

    def on_trade(self, pnl):
        self.reset_daily()
        self.daily_pnl += pnl

    def on_spike(self, pct):
        if abs(pct) > 0.03:
            self.event_pause_until = time.time() + 1800
            tg_send(f"⚡ 插针事件: 1分钟{pct*100:.1f}%, 暂停30分钟")

RISK = RiskEngine()

# ============================================================
# 统一结算 — 两个模式都用真实市场数据
# ============================================================
def _settle(r, st, side, sz):
    """
    模拟: 真实盘口一档价 + 真实费率(成交记录推导)
    实盘: 真实成交回查(实际成交均价 + OKX实扣手续费)
    """
    if DRY_RUN:
        px = get_book_px(st.inst, side)
        if px is None:
            tg_send(f"🚨 {st.inst} 盘口不可得, 成交取消(Fail-Safe)")
            return None
        rate = FeeProfile.rate()
        return {"px": px, "fee": px * sz * rate if rate else 0.0}
    fill = FillEngine.get_fill(r.get("ordId"), st.inst)
    if fill is None:
        tg_send(f"🚨 <b>{st.inst} 成交回查失败</b>\n订单{r.get('ordId')}\n"
                f"该币种已停机, 请人工核对!")
        st.g["running"] = False; st.s["running"] = False
        return None
    return fill

# ============================================================
# 自诊断 + 心跳
# ============================================================
class SelfCheck:
    def __init__(self):
        self.engine_err = 0
        self.api_fail = 0
        self.api_fail_ts = 0
        self.last_report = 0

    def engine_error(self, e):
        self.engine_err += 1
        log("engine:", e)
        if self.engine_err == 3:
            tg_send(f"🚨 <b>引擎连续异常×3</b>\n{e}\n请检查Render日志")

    def engine_ok(self):
        self.engine_err = 0

    def api_fail(self, where):
        now = time.time()
        if now - self.api_fail_ts > 600:
            self.api_fail = 0
        self.api_fail += 1
        self.api_fail_ts = now
        if self.api_fail == 5:
            tg_send(f"🚨 <b>OKX API 10分钟内失败×5</b>\n位置: {where}\n"
                    f"疑似限频/网络, 相关请求将Fail-Safe跳过")

    def hourly_report(self):
        if time.time() - self.last_report < 3600: return
        self.last_report = time.time()
        issues = []
        if LIVE.get() is None:
            issues.append(f"❌ 余额API失败: {LIVE.last_err}")
        with STRATS_LOCK:
            for inst, st in STRATS.items():
                if st.g["running"] and time.time() - st.g["last_px_ts"] > 300:
                    issues.append(f"⚠️ {inst} 行情超5分钟未更新")
                diff, real = PositionAudit.audit(st)
                if diff is not None and abs(diff) > 0.00001:
                    issues.append(f"⚠️ {inst} 对账差异{diff} (交易所{real})")
        if issues:
            tg_send("🩺 <b>自诊断报告</b>\n" + "\n".join(issues))

SELF = SelfCheck()
HEARTBEAT = {"ts": time.time()}

# ============================================================
# 网格引擎 — 每格真实买价, 回合盈亏=真实卖价−真实买价−双边费
# ============================================================
PYRAMID_LAYERS = [{"trigger": 0.03,  "mult": 1.0},
                  {"trigger": 0.05,  "mult": 1.5},
                  {"trigger": 0.075, "mult": 2.3},
                  {"trigger": 0.10,  "mult": 3.4}]

def _iv(st, p):
    if st.g["step"] in (None, 0) or st.g["lo"] is None: return 0
    return max(0, min(st.g["n"], int((p - st.g["lo"]) / st.g["step"])))

def grid_tick(st):
    if not st.g["running"]: return
    p = get_price(st.inst)
    if p is None:
        SELF.api_fail("ticker"); return
    st.g["last_px_ts"] = time.time()
    if st.g["step"] is None:
        st.g["step"] = DynamicParams.grid_step(st.inst)
        if st.g["step"] is None: return
    if not st.g["inv"] and time.time() - st.g["last_step_ts"] > 21600:
        new_step = DynamicParams.grid_step(st.inst)
        if new_step and abs(new_step - (st.g["step"] or 0)) / max(st.g["step"], 1) > 0.2:
            st.g["step"] = new_step
            tg_send(f"📊 {st.inst} ATR格距→{new_step:.0f}")
        st.g["last_step_ts"] = time.time()
    if st.g["last_iv"] is None:
        st.g["last_iv"] = _iv(st, p); return
    cur, last = _iv(st, p), st.g["last_iv"]
    if cur == last: return
    state = MarketState.detect(st.inst)
    # 🆕 浮亏用每格真实买价计算
    grid_float = sum((p - d["buy_px"]) * d["qty"]
                     for d in st.g["inv"].values()) * (st.g["sz"] or 0)
    if not RISK.check(grid_float, 0): return
    if cur < last and state != "TREND_DOWN":
        line = last
        if (line not in st.g["inv"]
                and sum(d["qty"] for d in st.g["inv"].values()) < st.g["max_inv"]
                and st.g["sz"] and st.g["sz"] * p <= st.cap):
            _g_trade(st, "buy", line)
    elif cur > last:
        if last in st.g["inv"]:
            _g_trade(st, "sell", last)
    st.g["last_iv"] = cur

def _g_trade(st, side, line):
    r = place_market(st.inst, side, st.g["sz"])
    if r.get("code") != "0":
        tg_send(f"❌ {st.inst} 下单失败: {r.get('msg')}")
        return False
    s = _settle(r, st, side, st.g["sz"])
    if s is None: return False
    px, fee = s["px"], s["fee"]
    st.g["fees"] += fee
    tag = "🧪" if DRY_RUN else ("🟢" if side == "buy" else "🔴")
    if side == "buy":
        st.g["inv"][line] = {"qty": 1, "buy_px": px, "fee": fee}  # 🆕 记真实买价
        tg_send(f"{tag} {st.inst} 买 第{line}格 @{px:,.2f} 费{fee:.4f}")
    else:
        d = st.g["inv"].pop(line, None)
        buy_px = d["buy_px"] if d else None
        buy_fee = d["fee"] if d else 0.0
        st.g["rounds"] += 1
        if buy_px is not None:
            # 🆕 回合盈亏 = 真实卖价 − 真实买价 − 双边真实手续费
            pnl = (px - buy_px) * st.g["sz"] - buy_fee - fee
        else:
            # 买价不可考(超历史深度) → 诚实标注, 用现金流卖出所得计
            pnl = px * st.g["sz"] - fee
            tg_send(f"⚠️ {st.inst} 第{line}格买价不可考, 本回合按卖出所得计")
        RISK.on_trade(pnl)
        avg_note = f"买{buy_px:,.2f}→卖{px:,.2f}" if buy_px else f"卖{px:,.2f}"
        tg_send(f"{tag} {st.inst} 卖 回合{st.g['rounds']} {avg_note} "
                f"净{pnl:+.2f}U")
    return True

# ============================================================
# 低吸引擎 (金字塔+移动止损)
# ============================================================
def swing_tick(st):
    if not st.s["running"]: return
    p = get_price(st.inst)
    if p is None:
        SELF.api_fail("ticker"); return
    st.g["last_px_ts"] = time.time()
    if st.s["base"] is None:
        st.s["base"] = p; return
    rsi_d  = IndicatorCache.get(Indicators.rsi, st.inst, "1D", 14)
    rsi_4h = IndicatorCache.get(Indicators.rsi, st.inst, "4H", 14)
    if rsi_d is None:
        SELF.api_fail("rsi"); return
    dip, rally, coef = DynamicParams.swing_params(st.inst)
    dev = (p - st.s["base"]) / st.s["base"]
    if st.s["pos"] > 0 and st.s["trail_stop"]:
        if p <= st.s["trail_stop"]:
            _sw_sell_all(st, p, "移动止损"); return
        if st.s["entry_avg"] and p > st.s["entry_avg"]:
            new_stop = p * 0.95
            if new_stop > st.s["trail_stop"]:
                st.s["trail_stop"] = new_stop
    if time.time() - st.s["last_ts"] < 600: return
    base_sz = st.cap * 0.12 / p if p > 0 else 0
    if (dev <= -dip and st.s["layer_idx"] < 4
            and rsi_d < 30 and (rsi_4h is None or rsi_4h > 40)):
        layer = PYRAMID_LAYERS[st.s["layer_idx"]]
        sz = round_to_lot(st, base_sz * layer["mult"] * coef)
        max_pos = st.cap / p
        if (sz >= (st.min_sz or 0) and st.s["pos"] + sz <= max_pos
                and sz * p <= st.cap and sz > 0):
            _sw_buy_layer(st, p, sz)
    elif dev >= rally and st.s["pos"] > 0:
        _sw_sell_all(st, p, "涨幅达标")

def _sw_buy_layer(st, p, sz):
    r = place_market(st.inst, "buy", sz)
    if r.get("code") != "0":
        tg_send(f"❌ {st.inst} 买入失败: {r.get('msg')}"); return
    s = _settle(r, st, "buy", sz)
    if s is None: return
    px = s["px"]
    st.s["layers"].append((sz, px))
    st.s["pos"] = round(st.s["pos"] + sz, 8)
    st.s["layer_idx"] += 1
    st.s["last_ts"] = time.time()
    st.s["entry_avg"] = sum(a*b for a, b in st.s["layers"]) / st.s["pos"]
    st.s["trail_stop"] = st.s["entry_avg"] * 0.95
    st.s["base"] = px
    tag = "🧪" if DRY_RUN else "🟢"
    tg_send(f"{tag} {st.inst} 金字塔第{st.s['layer_idx']}层 {sz} @{px:,.2f} "
            f"均价{st.s['entry_avg']:,.2f} 止损{st.s['trail_stop']:,.2f}")

def _sw_sell_all(st, p, reason):
    if st.s["pos"] <= 0: return
    amt = round_to_lot(st, st.s["pos"])
    if amt <= 0: return
    r = place_market(st.inst, "sell", amt)
    if r.get("code") != "0":
        tg_send(f"❌ {st.inst} 卖出失败: {r.get('msg')}"); return
    s = _settle(r, st, "sell", amt)
    if s is None: return
    pnl = (s["px"] - (st.s["entry_avg"] or s["px"])) * amt - s["fee"]
    RISK.on_trade(pnl)
    st.s["pnl_7d"] += pnl
    tag = "🧪" if DRY_RUN else "🔴"
    tg_send(f"{tag} {st.inst} 全平[{reason}] @{s['px']:,.2f} 净{pnl:+.2f}U")
    st.s.update({"pos": 0, "layers": [], "layer_idx": 0,
                 "entry_avg": None, "trail_stop": None, "base": s["px"],
                 "last_ts": time.time()})

# ============================================================
# Telegram 页面与命令
# ============================================================
def page_home():
    bal, tag = get_funds()
    if bal is None:
        return (f"🤖 <b>v3.6 引擎</b>\n\n⚠️ 余额API失败: {LIVE.last_err}\n"
                f"Fail-Safe: 交易暂停",
                [[btn("🔄 重试", "home")]])
    rate = FeeProfile.rate()
    t = (f"🤖 <b>OKX 多币策略引擎 v3.6</b>\n\n"
         f"💰 实时可用: {bal['avail']:.2f}U ({tag})\n"
         f"🪙 币种: {len(STRATS)}个\n"
         f"📅 当日真实盈亏: {RISK.daily_pnl:+.2f}U\n"
         f"🛡️ 熔断: L2{'🔴' if RISK.L2_float_stop else '🟢'} "
         f"L3{'🔴' if RISK.L3_daily_stop else '🟢'}\n"
         f"{'🧪 模拟(仅拦截下单)' if DRY_RUN else '⚠️ 实盘'}\n"
         f"费率: {f'{rate*100:.3f}%' if rate else '无历史成交'}")
    kb = [[btn("🪙 币种", "coins"), btn("💰 余额", "bal")],
          [btn("🛡️ 风控", "risk"), btn("🔄 刷新", "home")]]
    return t, kb

def page_coins():
    lines = []
    with STRATS_LOCK:
        for inst, st in STRATS.items():
            p = get_price(inst) or 0
            pos_v = st.s["pos"] * p
            # 浮亏按真实买价
            g_float = sum((p - d["buy_px"]) * d["qty"]
                          for d in st.g["inv"].values()) * (st.g["sz"] or 0) \
                      if st.g["sz"] else 0
            lines.append(
                f"<b>{inst}</b> {p:,.2f} {MarketState.label(MarketState.detect(inst))}\n"
                f"📐{'▶️' if st.g['running'] else '⏸️'}回合{st.g['rounds']} "
                f"格{len(st.g['inv'])}/{st.g['max_inv']} 浮{g_float:+.2f}U · "
                f"📉{'▶️' if st.s['running'] else '⏸️'}仓{pos_v:.1f}U "
                f"层{st.s['layer_idx']}/4\n"
                f"份额{st.cap:.0f}U")
    body = "\n\n".join(lines) if lines else "暂无币种\n发 <code>add BTC-USDT</code>"
    t = f"🪙 <b>币种管理</b>\n\n{body}\n\n命令: add/del/close/start/stop + 币种名"
    return t, [[btn("⬅️ 主页", "home")]]

def page_balance():
    bal, tag = get_funds()
    if bal is None:
        return (f"❌ 余额查询失败\n{LIVE.last_err}",
                [[btn("🔄 重试", "bal")], [btn("⬅️ 主页", "home")]])
    funding = LIVE.get_funding()
    t = (f"💰 <b>实时账户</b> {tag}\n\n"
         f"总权益: {bal['eq']:.2f} USDT\n"
         f"可用: {bal['avail']:.2f} USDT\n"
         f"冻结: {bal['frozen']:.2f} USDT\n"
         f"资金账户: {funding if funding is not None else '查询失败'} USDT\n\n"
         f"每币份额: {bal['avail']*0.9/max(len(STRATS),1):.1f}U × {len(STRATS)}币\n"
         f"L2线: {bal['eq']*0.08:.1f}U / L3线: {bal['eq']*0.04:.1f}U")
    kb = [[btn("🔄 刷新", "bal")]]
    if funding and funding > 10:
        kb.insert(0, [btn(f"⬆️ 划转{funding:.0f}U到交易账户", "tr_all")])
    kb.append([btn("⬅️ 主页", "home")])
    return t, kb

def page_risk():
    t = (f"🛡️ <b>风控面板</b>\n\n"
         f"L2 浮亏熔断(8%): {'🔴触发' if RISK.L2_float_stop else '🟢正常'}\n"
         f"L3 日亏熔断(4%): {'🔴触发' if RISK.L3_daily_stop else '🟢正常'}\n"
         f"插针暂停: {'是' if time.time()<RISK.event_pause_until else '否'}\n\n"
         f"当日真实盈亏: {RISK.daily_pnl:+.2f}U (交易所成交回放)\n"
         f"实时权益: {RISK.live_funds or 0:.2f}U\n"
         f"引擎异常计数: {SELF.engine_err}")
    return t, [[btn("🔄 解除L2", "rk_l2"), btn("🔄 解除L3", "rk_l3")],
               [btn("⬅️ 主页", "home")]]

PAGES = {"home": page_home, "coins": page_coins,
         "bal": page_balance, "risk": page_risk}

def on_cb(data, mid):
    if data in PAGES:
        t, kb = PAGES[data](); tg_send(t, kb, edit=mid)
    elif data == "rk_l2":
        RISK.L2_float_stop = False
        t, kb = page_risk(); tg_send(t, kb, edit=mid)
    elif data == "rk_l3":
        RISK.L3_daily_stop = False
        t, kb = page_risk(); tg_send(t, kb, edit=mid)
    elif data == "tr_all":
        f = LIVE.get_funding()
        if f and f > 0:
            r = LIVE.transfer_to_trading(f)
            tg_send("✅ 划转成功" if r.get("code") == "0"
                    else f"❌ 划转失败: {r.get('msg')}")
        t, kb = page_balance(); tg_send(t, kb, edit=mid)

def on_text(txt):
    t = txt.strip(); low = t.lower(); parts = t.split()
    if t in ("/menu", "/start"): tg_send(*page_home()); return
    if t == "/coins": tg_send(*page_coins()); return
    if t == "/balance": tg_send(*page_balance()); return
    if t == "/risk": tg_send(*page_risk()); return
    if len(parts) == 2:
        cmd, arg = low, parts[1].upper()
        if cmd.startswith("add "):   tg_send(add_symbol(arg)); return
        if cmd.startswith("del ") or cmd.startswith("delete "):
            tg_send(del_symbol(arg)); return
        if cmd.startswith("close "): tg_send(close_symbol(arg)); return
        if cmd.startswith("start "): tg_send(start_symbol(arg)); return
        if cmd.startswith("stop "):  tg_send(stop_symbol(arg)); return
    tg_send("💡 /menu · /coins · /balance · /risk\n"
            "add/del/close/start/stop + 币种名")

# ============================================================
# 主循环 + 看门狗 + TG轮询
# ============================================================
_last_audit = 0

def engine_loop():
    global _last_audit, TOTAL_AVAIL
    threading.current_thread().name = "engine"
    while True:
        try:
            HEARTBEAT["ts"] = time.time()
            bal, tag = get_funds()
            if bal is None or bal["avail"] < 1:
                log(f"⏸️ 资金不可用({tag}), 本轮跳过")
                time.sleep(30); continue
            RISK.update_funds(bal)
            TOTAL_AVAIL = bal["avail"]
            with STRATS_LOCK:
                items = list(STRATS.items())
            for inst, st in items:
                new_cap = bal["avail"] * 0.9 / max(len(items), 1)
                if abs(st.cap - new_cap) > 0.5:
                    st.cap = new_cap
                    pp = get_price(inst)
                    if pp: st.autofit(pp)
                grid_tick(st)
                swing_tick(st)
            if time.time() - _last_audit > 600:
                _last_audit = time.time()
                for inst, st in items:
                    diff, real = PositionAudit.audit(st)
                    if diff is not None and abs(diff) > 0.00001:
                        tg_send(f"⚠️ <b>{inst} 持仓对账异常</b>\n"
                                f"交易所={real} 差异={diff}\n请核查!")
            SELF.engine_ok()
            SELF.hourly_report()
        except Exception as e:
            SELF.engine_error(e)
        time.sleep(10)

def watchdog_loop():
    alerted = False
    while True:
        try:
            if time.time() - HEARTBEAT["ts"] > 120 and not alerted:
                tg_send("🚨 <b>引擎心跳消失>120秒</b>\n引擎线程疑似死亡!")
                alerted = True
            elif time.time() - HEARTBEAT["ts"] <= 120:
                alerted = False
        except Exception: pass
        time.sleep(30)

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
    with STRATS_LOCK:
        coins = {i: {"grid": s.g["running"], "swing": s.s["running"],
                     "rounds": s.g["rounds"],
                     "grid_lines": len(s.g["inv"]),
                     "pos": s.s["pos"]}
                 for i, s in STRATS.items()}
    return jsonify({"status": "ok", "dry_run": DRY_RUN,
                    "heartbeat_age": round(time.time() - HEARTBEAT["ts"], 1),
                    "live_funds": RISK.live_funds,
                    "daily_pnl_real": RISK.daily_pnl,
                    "fee_rate": FeeProfile.rate(),
                    "risk": {"L2": RISK.L2_float_stop, "L3": RISK.L3_daily_stop},
                    "coins": coins})

if __name__ == "__main__":
    tg_send("🤖 <b>v3.6 启动</b>\n"
            "数据铁律: 全链路交易所实时数据\n"
            "回合盈亏=真实买价→真实卖价−双边真实手续费")
    ConfigStore.load_coins()                       # 1. 恢复币种列表
    with STRATS_LOCK:
        for st in STRATS.values():                 # 2. 就绪区间/格距
            p = get_price(st.inst)
            if p and not st.g["lo"]:
                st.g["lo"], st.g["hi"] = round(p*0.9), round(p*1.1)
                st.autofit(p)
            st.g["step"] = DynamicParams.grid_step(st.inst)
    RebuildEngine.full_rebuild()                   # 3. 交易所成交记录回放重建
    threading.Thread(target=engine_loop, daemon=True).start()
    threading.Thread(target=watchdog_loop, daemon=True).start()
    threading.Thread(target=tg_poll_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
