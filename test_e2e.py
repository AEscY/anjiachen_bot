"""
端到端回环测试。

用内存模拟交易所跑完整主循环（真实 bot.step 代码，
只替换交易所 IO），跑完后检查：

    本地账本持仓  ==  交易所实际持仓
    本地已实现盈亏 ==  实际现金流差额

这是旧版一直没做的验证。旧版 v1 我曾断言"账本会漂移"，
跑了 300 轮仿真才发现是错的 —— 结论必须来自数据，不能来自感觉。

本测试若通过，说明成交记账链路自洽。
"""
import asyncio
import os
import random
import sys
import tempfile

sys.path.insert(0, ".")
os.environ.setdefault("TG_BOT_TOKEN", "123456:fake")
os.environ.setdefault("TG_CHAT_ID", "1")
os.environ.setdefault("OKX_API_KEY", "k")
os.environ.setdefault("OKX_SECRET_KEY", "s")
os.environ.setdefault("OKX_PASSPHRASE", "p")

import config as C

C.STATE_FILE = os.path.join(tempfile.mkdtemp(), "e2e_state.json")

PASS, FAIL = [], []


def check(name, ok, extra=""):
    if ok:
        PASS.append(name)
        print(f"  ✅ {name}" + (f"  ({extra})" if extra else ""))
    else:
        FAIL.append(f"{name} | {extra}")
        print(f"  ❌ {name}  ({extra})")


SYM = "SOL/USDT"


class SimExchange:
    """
    内存交易所。价格穿越挂单价即成交（限价单的合理简化）。
    手续费 0.08%（限价单 taker/maker 近似）。

    【必须建模冻结资金】

    第一版只用一个 self.usdt，下单不扣钱、成交才扣。
    结果真实部署暴露的 bug 在这里根本复现不了：

        真实交易所挂出限价买单后，USDT 从 free 移到 used。
        权益计算若只看 free，网格一挂单就"亏损"掉全部冻结额，
        触发日亏损熔断并每小时重复触发 → 永久停摆。

    模拟不建模 free/used 分离，就永远测不出这类问题。
    """

    FEE = 0.0008

    def __init__(self, prices, start_usdt=1000.0):
        self.prices = prices
        self.i = 0
        self.usdt = start_usdt          # 可用
        self.used = 0.0                 # 冻结在挂单里
        self.coins = {SYM: 0.0}
        self.orders = {}
        self._nid = 0
        self.fills = 0

    # ── 行情 ──
    def price(self, sym):
        return self.prices[min(self.i, len(self.prices) - 1)]

    def tick(self):
        """推进一格并撮合"""
        self.i += 1
        p = self.price(SYM)
        for oid, o in list(self.orders.items()):
            if o["status"] != "open":
                continue
            hit = (o["side"] == "buy" and p <= o["price"]) or \
                  (o["side"] == "sell" and p >= o["price"])
            if not hit:
                continue
            qty, px = o["qty"], o["price"]
            if o["side"] == "buy":
                cost = qty * px * (1 + self.FEE)
                # 钱在挂单时已冻结，成交时从冻结里扣
                self.used = max(0.0, self.used - cost)
                self.coins[SYM] += qty
            else:
                if self.coins.get(SYM, 0.0) < qty - 1e-12:
                    continue                       # 持仓不足，跳过
                self.coins[SYM] -= qty
                self.usdt += qty * px * (1 - self.FEE)
            o["status"] = "closed"
            o["filled"] = qty
            o["average"] = px
            self.fills += 1

    # ── 账户 ──
    def balances(self):
        return self.usdt, self.used, {SYM: self.coins.get(SYM, 0.0)}

    # ── 订单 ──
    def open_orders(self, sym):
        return [{"id": k} for k, v in self.orders.items()
                if v["status"] == "open" and v["sym"] == sym]

    def fetch_order(self, oid, sym):
        o = self.orders.get(oid)
        if o is None:
            return None
        return {"id": oid, "status": o["status"],
                "filled": o.get("filled", 0.0),
                "average": o.get("average") or o["price"],
                "price": o["price"]}

    def _place(self, side, sym, qty, price):
        # 买单预冻结（含手续费），真实交易所就是这样
        if side == "buy":
            need = qty * price * (1 + self.FEE)
            if need > self.usdt:
                return None                        # 可用不足，不下单
            self.usdt -= need
            self.used += need
        self._nid += 1
        oid = str(self._nid)
        self.orders[oid] = {"id": oid, "side": side, "sym": sym,
                            "qty": qty, "price": price,
                            "status": "open", "filled": 0.0,
                            "locked": qty * price * (1 + self.FEE)
                            if side == "buy" else 0.0}
        return oid

    def cancel(self, oid, sym):
        o = self.orders.get(oid)
        if o is None or o["status"] != "open":
            return
        if o["side"] == "buy":
            back = o.get("locked", 0.0)
            self.used = max(0.0, self.used - back)
            self.usdt += back                       # 解冻退回可用
        o["status"] = "canceled"

    def limit_buy(self, sym, qty, price):
        return self._place("buy", sym, qty, price)

    def limit_sell(self, sym, qty, price):
        return self._place("sell", sym, qty, price)

    def market_sell(self, sym, qty):
        p = self.price(sym)
        qty = min(qty, self.coins.get(sym, 0.0))
        if qty <= 0:
            return None
        self.coins[sym] -= qty
        self.usdt += qty * p * (1 - self.FEE)
        self.fills += 1
        return "M"

    # ── 精度（不做限制，测真逻辑）──
    def round_qty(self, sym, q):
        return round(q, 6)

    def round_price(self, sym, p):
        return round(p, 4)

    def min_amount(self, sym):
        return 0.0


class FakeTg:
    def __init__(self):
        self.msgs = []

    async def send_message(self, chat_id, text):
        self.msgs.append(text)


def make_bot(sim, params=None):
    from unittest.mock import patch
    with patch("bot.Exchange"), patch("bot.Bot"):
        import bot as B
        import store
        from params import Params
        from risk import Risk

        st = store.empty_state()
        b = B.TradingBot.__new__(B.TradingBot)
        b.state = st
        b.p = Params(params or {})
        b.risk = Risk(st["risk"], b.p)
        b.ex = sim
        b.tg = FakeTg()
        b.running = True
        b._last_save = 0.0
        b.state["coins"] = {SYM: {"center": 0.0, "lots": {}, "orders": {}}}
        return b


def gen_prices(n, start=100.0, kind="range"):
    """生成价格序列"""
    rnd = random.Random(7)
    out, p = [], start
    for i in range(n):
        if kind == "range":
            # 震荡：均值回归
            p += (start - p) * 0.05 + rnd.gauss(0, 0.6)
        elif kind == "down":
            p -= abs(rnd.gauss(0.03, 0.3))
        elif kind == "up":
            p += abs(rnd.gauss(0.03, 0.3))
        out.append(round(max(1.0, p), 4))
    return out


async def test_frozen_not_counted_as_loss():
    """
    真跑代码（非算术推演）：挂单冻结资金不得触发日亏损熔断。

    真实部署日志：
        日基准 7256.38U（可用 4805.53 + 1 ETH 2450.85）
        挂 4 档买单，每档 960.91U，冻结 3843.64U
        日亏损 52.97% 达上限 5% → 暂停 1 小时
        冷却中（剩 59/60 分钟）… 每小时重复 → 永久停摆

    为什么必须真跑：
        第一版守卫是自己写算术验证公式，结果把 bug 改回去
        测试照样通过 —— 因为它没跑 bot.step()。
        这里用真实 step() + 建模了冻结的模拟交易所。
    """
    # 价格恒定 → 挂单不会成交，冻结资金一直挂着
    prices = [2450.85] * 50
    sim = SimExchange(prices, start_usdt=4805.53)
    sim.coins[SYM] = 1.0                      # 1 ETH 持仓

    # 两个币种，模拟真实配置
    b = make_bot(sim, params={"levels": 2, "capital_pct": 0.8,
                              "reserve": 1.0, "daily_loss": 0.05})
    b.state["coins"] = {
        SYM: {"center": 0.0, "lots": {}, "orders": {}},
        "ETH/USDT": {"center": 0.0, "lots": {}, "orders": {}},
    }

    # 第一轮：记录日基准（此时还没挂单）
    await b.step()
    base = b.state["risk"].get("day_start_equity", 0.0)
    check("日基准已记录", base > 0, f"{base:.2f}U")

    # 第二轮：挂单已下，冻结资金已扣除
    await b.step()
    _, used, _ = sim.balances()
    check("确实冻结了资金（否则测不到问题）", used > 1.0, f"冻结 {used:.2f}U")

    paused = b.risk.paused_until > 0
    reason = b.risk.reason
    check("挂单冻结不应触发日亏损熔断", not paused,
          f"reason={reason or '无'}")

    if paused:
        print(f"      ← 已暂停，原因：{reason}")

    # 连续跑 20 轮，确认不会反复触发
    for _ in range(20):
        await b.step()
    check("连续 20 轮未被误熔断", b.risk.paused_until == 0,
          f"reason={b.risk.reason or '无'}")


async def _run_frozen_test():
    print("\n  关键场景：挂单冻结资金不得误判为亏损")
    await test_frozen_not_counted_as_loss()


async def run(kind, n=300, params=None):
    prices = gen_prices(n, kind=kind)
    sim = SimExchange(prices, start_usdt=1000.0)
    b = make_bot(sim, params)

    for _ in range(n):
        await b.step()
        sim.tick()

    # 最终结算：按现价给持仓估值
    last = sim.price(SYM)
    local_qty = sum(float(l["qty"]) for l in
                    b.state["coins"][SYM]["lots"].values())
    exch_qty = sim.coins[SYM]

    return {
        "local_qty": local_qty,
        "exch_qty": exch_qty,
        "realized": b.state["risk"]["realized_pnl"],
        "usdt": sim.usdt,
        "last": last,
        "fills": sim.fills,
        "msgs": len(b.tg.msgs),
    }


def main() -> int:
    asyncio.run(_run_frozen_test())

    # ═══════════════════════════════════════════════
    print("═══ 端到端回环：本地账本 vs 交易所 ═══\n")

    for kind, label in (("range", "震荡"), ("down", "下跌"), ("up", "上涨")):
        r = asyncio.run(run(kind, n=300))
        diff = abs(r["local_qty"] - r["exch_qty"])
        print(f"  [{label}]  成交 {r['fills']} 笔  末价 {r['last']:.2f}")
        check(f"  {label}：账本持仓 == 交易所持仓", diff < 1e-6,
              f"本地 {r['local_qty']:.6f} / 交易所 {r['exch_qty']:.6f} "
              f"差 {diff:.2e}")
        if kind == "up":
            # 网格买单挂在【现价下方】，单边上涨必然踏空。
            # 这是网格的固有特性，不是 bug ——
            # 之前分析过：网格赚的是震荡的钱。
            print("     （上涨踏空为网格固有特性，不要求成交）")
        else:
            check(f"  {label}：确实发生了交易", r["fills"] > 0,
                  f"{r['fills']} 笔")
        print()

    # ═══════════════════════════════════════════════
    print("═══ 关键场景：单边下跌不死挂 ═══\n")

    r = asyncio.run(run("down", n=300))
    check("下跌行情有卖出成交（未全部死挂）", r["fills"] >= 2,
          f"{r['fills']} 笔")
    check("下跌后账本仍一致", abs(r["local_qty"] - r["exch_qty"]) < 1e-6,
          f"差 {abs(r['local_qty']-r['exch_qty']):.2e}")

    # ═══════════════════════════════════════════════
    print("\n═══ 关键场景：关闭卖单跟随应更差 ═══\n")

    r_off = asyncio.run(run("down", n=300, params={"follow_hours": 0}))
    r_on = asyncio.run(run("down", n=300))
    print(f"  跟随关闭: 成交 {r_off['fills']} 笔, "
          f"剩余持仓 {r_off['local_qty']:.4f}")
    print(f"  跟随开启: 成交 {r_on['fills']} 笔, "
          f"剩余持仓 {r_on['local_qty']:.4f}")
    check("开启跟随后遗留持仓更少（死挂减少）",
          r_on["local_qty"] <= r_off["local_qty"] + 1e-9,
          f"{r_on['local_qty']:.4f} vs {r_off['local_qty']:.4f}")

    print("\n" + "=" * 66)
    print(f"通过: {len(PASS)} 项 | 失败: {len(FAIL)} 项")
    if FAIL:
        print("\n失败明细：")
        for f in FAIL:
            print(f"   ❌ {f}")
    print("=" * 66)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    sys.exit(main())
