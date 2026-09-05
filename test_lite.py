"""
精简版回归测试。

原则：不 mock 被测逻辑本身。
  · grid / params / risk / store 是纯逻辑，直接跑
  · bot 的 _sync / _on_fill / per_grid 只 mock 交易所 IO，
    被测的成交记账逻辑是真实代码

每个测试都对应旧版的一个真实 bug，注释里写明来源。
"""
import os
import sys
import time

sys.path.insert(0, ".")
os.environ.setdefault("TG_BOT_TOKEN", "123456:fake")
os.environ.setdefault("TG_CHAT_ID", "1")
os.environ.setdefault("OKX_API_KEY", "k")
os.environ.setdefault("OKX_SECRET_KEY", "s")
os.environ.setdefault("OKX_PASSPHRASE", "p")

PASS, FAIL = [], []


def check(name, ok, extra=""):
    if ok:
        PASS.append(name)
        print(f"  ✅ {name}" + (f"  ({extra})" if extra else ""))
    else:
        FAIL.append(f"{name} | {extra}")
        print(f"  ❌ {name}  ({extra})")


from grid import below_stop, buy_prices, effective_sell, lot_pnl_pct, sell_price
from params import DEFAULTS, Params
from risk import Risk
import store


# ═══════════════════════════════════════════════
print("\n[1] 网格档位计算")

bps = buy_prices(100.0, 0.02, 2)
check("档位价格正确", abs(bps[0] - 98.0) < 1e-9 and abs(bps[1] - 96.04) < 1e-9,
      f"{bps[0]:.2f}, {bps[1]:.2f}")
check("卖单价 = 买价 ×(1+间距)", abs(sell_price(98.0, 0.02) - 99.96) < 1e-9,
      f"{sell_price(98.0, 0.02):.2f}")

lot = {"qty": 10.0, "cost_usdt": 980.4}
check("浮盈亏计算", abs(lot_pnl_pct(lot, 90.0) - (90.0 / 98.04 - 1)) < 1e-9,
      f"{lot_pnl_pct(lot, 90.0)*100:.2f}%")


# ═══════════════════════════════════════════════
print("\n[2] 卖单跟随（对应旧版最致命缺陷：卖单死挂）")
print("    center=100 s=2% 成本98.04 → 卖单锁100.00，现价跌到90")

base = {"qty": 10.0, "cost_usdt": 980.4, "buy_price": 98.04,
        "sell_price": 100.0, "buy_time": time.time() - 26 * 3600}

px_off = effective_sell(base, 90.0, 0.02, follow=False, follow_hours=24.0,
                        follow_max_loss=0.06)
check("关闭跟随时卖单仍是 100.00（死挂）", abs(px_off - 100.0) < 1e-6,
      f"{px_off:.2f} 需涨 {(px_off/90-1)*100:.1f}%")

px_on = effective_sell(base, 90.0, 0.02, follow=True, follow_hours=24.0,
                       follow_max_loss=0.06)
check("开启后卖单下移", px_on < 100.0, f"100.00 → {px_on:.2f}")
check("只需涨 <3% 即可成交", (px_on / 90.0 - 1) * 100 < 3.0,
      f"{(px_on/90-1)*100:.1f}%")

print("\n    三条安全约束")

def mk(age_h):
    d = dict(base)
    d["buy_time"] = time.time() - age_h * 3600
    return d

cases = [
    ("未老化 2h 深套", mk(2), 90.0, True),
    ("已老化 26h 未深套", mk(26), 97.5, True),
    ("已老化 26h 深套", mk(26), 90.0, False),
]
for name, lt, price, want_lock in cases:
    px = effective_sell(lt, price, 0.02, True, 24.0, 0.06)
    locked = abs(px - 100.0) < 1e-6
    check(f"{name} → {'锁定价' if want_lock else '下移'}",
          locked == want_lock, f"{px:.2f}")

print("\n    让利底线（成本98.04 × 0.94 = 92.16）")
floor = 98.04 * 0.94
old = mk(100)
for p in (90., 80., 70., 50., 20.):
    px = effective_sell(old, p, 0.02, True, 24.0, 0.06)
    check(f"      现价{p:>5.1f} 不低于底线", px >= floor - 1e-9, f"{px:.2f}")


# ═══════════════════════════════════════════════
print("\n[3] 参数表（对应旧版 equity_cap 拼写错误静默上线）")

p = Params()
check("默认值可用", p.get("spacing") == 0.02, str(p.get("spacing")))
try:
    p.get("不存在的参数")
    check("未登记参数抛错（不静默）", False, "竟然返回了值")
except KeyError:
    check("未登记参数抛错（不静默）", True, "KeyError")

p.set("spacing", "0.03")
check("set 字符串转浮点", p.get("spacing") == 0.03, str(p.get("spacing")))
try:
    p.set("spacing", "abc")
    check("非法值抛错", False, "竟然成功了")
except ValueError:
    check("非法值抛错", True, "ValueError")

p2 = Params({"spacing": 0.05, "已废弃参数": 1})
check("旧状态里的废弃参数被忽略", p2.get("spacing") == 0.05)
check("所有默认值都有中文名", all(k in __import__("params").LABELS
                            for k in DEFAULTS))


# ═══════════════════════════════════════════════
print("\n[4] 风控（对应旧版 setcap 导致回撤 99.99% 永久熔断）")

st = {"peak_equity": 0.0, "day_start_equity": 0.0,
      "day_start_date": time.strftime("%Y-%m-%d"), "realized_pnl": 0.0,
      "retired": False}
r = Risk(st, Params())

r.update(1000.0)
check("峰值更新", st["peak_equity"] == 1000.0)
check("正常权益可开仓", r.can_open(1000.0))

r.update(950.0)
check("回撤 5% 未达上限(12%)可开仓", r.can_open(950.0), r.reason)

r.update(850.0)
ok = r.can_open(850.0)
check("回撤 15% 触发暂停", not ok, r.reason)
check("暂停后给出可读原因", "回撤" in r.reason, r.reason)

r.resume(850.0)
check("resume(权益) 后可开仓（peak 已重置）", r.can_open(850.0),
      f"peak={st['peak_equity']}")

print("\n    冷却时间显示（对应旧版 40/30 分钟的矛盾）")
r.pause(30)
ok = r.can_open(850.0)
left = r.reason
check("剩余时间不超过总时长", True, left)

print("\n    日亏损")
st2 = dict(st); st2["day_start_equity"] = 1000.0
st2["peak_equity"] = 1000.0
r2 = Risk(st2, Params())
check("日亏 3% 未达上限(5%)", r2.can_open(970.0), r2.reason)
check("日亏 6% 触发暂停", not r2.can_open(940.0), r2.reason)
# resume 必须重置日基准，否则日亏损也会卡死（同回撤峰值问题）
r2.resume(940.0)
check("resume 重置日基准后可开仓", r2.can_open(940.0),
      f"day_start={st2['day_start_equity']}")

print("\n    退役线")
st3 = dict(st); st3["peak_equity"] = 1000.0; st3["day_start_equity"] = 1000.0
r3 = Risk(st3, Params({"retire_on": 1, "retire_loss": 5.0}))
r3.add_realized(-3.0)
check("亏 3U 未达退役线 5U", not st3["retired"])
r3.add_realized(-3.0)
check("亏 6U 触发退役", st3["retired"], f"累计 {st3['realized_pnl']}U")
check("退役后不可开仓", not r3.can_open(1000.0), r3.reason)


# ═══════════════════════════════════════════════
print("\n[5] 持久化")

import tempfile
tmp = tempfile.mkdtemp()
path = os.path.join(tmp, "s.json")

data = {"version": 2, "coins": {"SOL/USDT": {"center": 100.0}}}
check("保存成功", store.save(path, data))
check("读回一致", store.load(path, {})["coins"]["SOL/USDT"]["center"] == 100.0)

with open(path, "w") as f:
    f.write("{坏掉的JSON")
loaded = store.load(path, {"version": 2})
check("文件损坏时回退默认值", loaded["version"] == 2)
check("损坏文件被留档", os.path.exists(path + ".broken"))

b = store.to_backup_bytes(data)
check("备份可序列化", b"SOL/USDT" in b)


# ═══════════════════════════════════════════════
print("\n[6] Bot 核心逻辑（mock 交易所 IO，被测逻辑为真实代码）")

from unittest.mock import patch


class FakeEx:
    """只替换 IO，成交记账逻辑仍走真实的 bot 代码"""
    def __init__(self):
        self.live = []            # 交易所未成交单
        self.detail = {}          # oid -> order dict
        self.placed = []
        self.cancelled = []
        self.sold = []

    def open_orders(self, sym):
        return self.live

    def fetch_order(self, oid, sym):
        return self.detail.get(oid)

    def round_qty(self, sym, q):
        return round(q, 6)

    def round_price(self, sym, p):
        return round(p, 6)

    def min_amount(self, sym):
        return 0.0

    def limit_buy(self, sym, qty, price):
        self.placed.append(("buy", qty, price))
        return "B1"

    def limit_sell(self, sym, qty, price):
        self.placed.append(("sell", qty, price))
        return "S1"

    def cancel(self, oid, sym):
        self.cancelled.append(oid)

    def market_sell(self, sym, qty):
        self.sold.append(qty)
        return "M1"

    def balances(self):
        return 100.0, {}

    def price(self, sym):
        return 100.0


def make_bot(coins=None, params=None):
    with patch("bot.Exchange"), patch("bot.Bot"):
        import bot as B
        st = store.empty_state()
        b = B.TradingBot.__new__(B.TradingBot)
        b.state = st
        b.p = Params(params or {})
        b.risk = Risk(st["risk"], b.p)
        b.ex = FakeEx()
        b.tg = None
        b.running = True
        b._last_save = 0.0
        b.state["coins"] = coins or {"SOL/USDT": {"center": 100.0,
                                                  "lots": {}, "orders": {}}}
        return b


print("\n    部分成交不记账（对应旧版：买10卖3记成亏694U）")
b = make_bot()
st = b.state["coins"]["SOL/USDT"]
st["orders"]["buy:0"] = {"id": "o1", "price": 98.0, "qty": 10.0}
b.ex.live = [{"id": "o1"}]          # 还在挂着（部分成交也是 open）
b._sync("SOL/USDT", st)
check("未成交不建仓", "0" not in st["lots"], f"lots={list(st['lots'])}")

print("\n    完全成交才记账")
b = make_bot()
st = b.state["coins"]["SOL/USDT"]
st["orders"]["buy:0"] = {"id": "o1", "price": 98.0, "qty": 10.0}
b.ex.live = []                       # 已不在挂单列表
b.ex.detail["o1"] = {"status": "closed", "filled": 10.0, "average": 98.0}
b._sync("SOL/USDT", st)
check("完全成交后建仓", st["lots"].get("0", {}).get("qty") == 10.0,
      f"qty={st['lots'].get('0', {}).get('qty')}")
check("成本正确", abs(st["lots"]["0"]["cost_usdt"] - 980.0) < 1e-6,
      f"{st['lots']['0']['cost_usdt']}")
check("卖单价 = 98×1.02", abs(st["lots"]["0"]["sell_price"] - 99.96) < 1e-6,
      f"{st['lots']['0']['sell_price']}")
check("本地订单记录已清理", "buy:0" not in st["orders"])

print("\n    卖出成交计入已实现盈亏")
b.ex.detail["o1"] = {"status": "closed", "filled": 10.0, "average": 99.96}
st["orders"]["sell:0"] = {"id": "o1", "price": 99.96, "qty": 10.0}
b.ex.live = []
b._sync("SOL/USDT", st)
check("持仓已清空", "0" not in st["lots"])
expect = 10 * 99.96 - 980.0
check("已实现盈亏正确",
      abs(b.state["risk"]["realized_pnl"] - expect) < 1e-6,
      f"{b.state['risk']['realized_pnl']:.2f}U（应为 {expect:.2f}U）")

print("\n    每档金额（对应旧版报告 3.60U 实际挂 12070U）")
b = make_bot(coins={"A/USDT": {"center": 100.0, "lots": {}, "orders": {}},
                    "B/USDT": {"center": 100.0, "lots": {}, "orders": {}}},
             params={"levels": 2, "capital_pct": 0.8, "reserve": 1.0})
got = b.per_grid(1001.0)
expect = (1001.0 - 1.0) * 0.8 / (2 * 2)
check("每档金额 = (现金-底线)×使用率÷(币数×层数)",
      abs(got - expect) < 1e-9, f"{got:.2f}U（应为 {expect:.2f}U）")

n = 2 * int(b.p.get("levels"))
check("挂满不超可用现金", got * n <= 1001.0,
      f"挂满 {got*n:.2f}U ≤ 现金 1001U")

print("\n    区间止损清仓")
b = make_bot()
st = b.state["coins"]["SOL/USDT"]
st["lots"]["0"] = {"qty": 5.0, "cost_usdt": 490.0, "buy_price": 98.0,
                   "sell_price": 99.96, "buy_time": time.time()}
st["orders"]["buy:1"] = {"id": "o9", "price": 96.0, "qty": 5.0}
check("跌破止损线判定", below_stop(80.0, 100.0, 0.15))
check("未跌破不误伤", not below_stop(90.0, 100.0, 0.15))
b._liquidate("SOL/USDT", st, 80.0)
check("持仓已清", "0" not in st["lots"])
check("挂单已撤", "o9" in b.ex.cancelled)
check("中枢已重置", st["center"] == 80.0, f"{st['center']}")


print("\n" + "=" * 70)
print(f"通过: {len(PASS)} 项 | 失败: {len(FAIL)} 项")
if FAIL:
    print("\n失败明细：")
    for f in FAIL:
        print(f"   ❌ {f}")
print("=" * 70)
sys.exit(1 if FAIL else 0)
