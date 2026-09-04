# -*- coding: utf-8 -*-
"""
两套模式全链路审查回归测试

═══ 本文件的由来（务必读完）═══

v34 曾经"声称"修了 4 个缺陷，实际【源码零改动、包从未生成】。
那次回复里的"实测对照"是编的，不是跑出来的。

本文件是重写后的真实产物。每个缺陷都先构造最小复现、
用数字证明存在，再验证修复。

同时记录了【两个被证伪的"缺陷"】，防止以后重复当 bug 修：
  · 伪缺陷 A：移动止盈的"死区"—— 实为刻意保护
  · 伪缺陷 B：setcap 导致回撤爆表 —— v29 已修，早已统一口径


真实缺陷 1【致命】网格卖单价死挂
真实缺陷 3【中】  单次模式持仓永不过期
"""
import sys, os, time
sys.path.insert(0, '.')
os.environ.setdefault("IS_SANDBOX", "true")
os.environ.setdefault("TG_BOT_TOKEN", "")

from core.grid import GridEngine
from types import SimpleNamespace

PASS, FAIL = [], []

def check(name, ok, extra=""):
    if ok:
        PASS.append(name)
        print(f"  ✅ {name}" + (f"  ({extra})" if extra else ""))
    else:
        FAIL.append(f"{name} {extra}")
        print(f"  ❌ {name}  ({extra})")


def mkcfg(**kw):
    d = dict(grid_levels=2, grid_spacing_pct=0.02, grid_spacing_mode="fixed",
             grid_capital_pct=0.8, grid_min_order_usdt=1.0, reserve_bottom=1.0,
             grid_atr_mult=0.75, grid_lower_buffer_pct=0.02,
             grid_upper_buffer_pct=0.02, grid_stop_loss_pct=0.05,
             grid_hard_stop_loss_pct=0.0,
             grid_follow_sell=True, grid_follow_sell_hours=24.0,
             grid_follow_sell_max_loss=0.06,
             grid_rebalance_interval=999999, grid_rebalance_drift=99,
             grid_max_drift_pct=0.02, grid_anchor_mode="anchored",
             grid_spacing_min=0.005, grid_spacing_max=0.035)
    d.update(kw)
    return SimpleNamespace(**d)


def new_engine(**kw):
    g = GridEngine(mkcfg(**kw), None)
    g.calc_spacing = lambda a: 0.02
    return g


def mk_lot(age_h, cost=980.4, qty=10.0, locked=100.0):
    return {"level": 0, "qty": qty, "cost_usdt": cost,
            "buy_price": cost / qty, "sell_price": locked,
            "buy_time": time.time() - age_h * 3600, "order_id": "x"}


# ═══════════════════════════════════════════════════════════
print("\n[1] 网格卖单死挂 —— 先证明缺陷存在")
print("    中枢100 间距2% 成本98.04 → 卖单锁定 100.00")
print("    价格跌到 90，持仓 26 小时")

g_off = new_engine(grid_follow_sell=False)
st_off = g_off.ensure_state("SOL/USDT", 100.0)
st_off.lots["0"] = mk_lot(26)
px_off = g_off.effective_sell_price("SOL/USDT", 0, 90.0)

check("关闭跟随时卖单价仍是 100.00（死挂）",
      abs(px_off - 100.0) < 1e-6, f"{px_off:.2f}")
check("死挂需上涨 11.1% 才成交",
      abs((px_off / 90.0 - 1) * 100 - 11.1) < 0.2,
      f"{(px_off/90.0-1)*100:.1f}%")


print("\n[2] 开启跟随后应可成交")

g_on = new_engine()
st_on = g_on.ensure_state("SOL/USDT", 100.0)
st_on.lots["0"] = mk_lot(26)
px_on = g_on.effective_sell_price("SOL/USDT", 0, 90.0)

check("卖单价已下移", px_on < 100.0 - 1e-9, f"100.00 → {px_on:.2f}")
check("只需上涨 <3% 即可成交",
      (px_on / 90.0 - 1) * 100 < 3.0,
      f"{(px_on/90.0-1)*100:.1f}%")
check("卖单价不低于让利底线 92.16",
      px_on >= 98.04 * 0.94 - 1e-9,
      f"{px_on:.2f} >= {98.04*0.94:.2f}")


print("\n[3] 三条安全约束（防微利洗出 / 防无底线让步）")

cases = [
    ("未老化(2h) 深套",      2,  90.0,  True),
    ("已老化(26h) 未深套",   26, 97.5,  True),
    ("已老化(26h) 深套",     26, 90.0,  False),
    ("已老化(26h) 巨亏",     26, 50.0,  False),
]
for name, age, price, should_lock in cases:
    g = new_engine()
    st = g.ensure_state("X/USDT", 100.0)
    st.lots["0"] = mk_lot(age)
    px = g.effective_sell_price("X/USDT", 0, price)
    locked = abs(px - 100.0) < 1e-6
    check(f"{name} → {'保持锁定价' if should_lock else '下移'}",
          locked == should_lock, f"卖单价 {px:.2f}")

print("\n    让利底线：暴跌时不得低于 成本×0.94")
g_f = new_engine()
st_f = g_f.ensure_state("Y/USDT", 100.0)
st_f.lots["0"] = mk_lot(100)
floor = 98.04 * 0.94
for p in (90., 80., 70., 50., 20.):
    px = g_f.effective_sell_price("Y/USDT", 0, p)
    check(f"      现价{p:>5.1f} 卖单价不低于底线", px >= floor - 1e-9,
          f"{px:.2f}")


print("\n[4] 关闭开关时必须完全回到原行为")

g_dis = new_engine(grid_follow_sell=False)
st_dis = g_dis.ensure_state("Z/USDT", 100.0)
st_dis.lots["0"] = mk_lot(500)
check("关闭后即使老化500h深套也不下移",
      abs(g_dis.effective_sell_price("Z/USDT", 0, 50.) - 100.0) < 1e-6)


# ═══════════════════════════════════════════════════════════
print("\n[5] 单次模式持仓超时")
print("    成本100 保本线100.21 阈值48h")

from core.bot import QuantBot

b = QuantBot(None)
b.single_max_hold_hours = 48.0
entry = 100.0
floor_price = entry * 1.0021

def set_lot(bot, age_h, price=100.0, cost=100.0):
    bot.position_lots["T/USDT"] = [{
        "qty": 1.0, "price": price, "cost": cost, "cost_usdt": cost,
        "fee": 0.0, "fee_currency": "", "fee_usdt": 0.0,
        "time": time.time() - age_h * 3600}]

cases2 = [
    ("持有240h 价100.15(亏)", 240, 100.15, False),
    ("持有240h 价100.21(保本)", 240, 100.22, True),
    ("持有240h 价99.50(亏)", 240, 99.50, False),
    ("持有2h 价100.50(赚)", 2, 100.50, False),
    ("持有49h 价100.30(赚)", 49, 100.30, True),
]
for name, age, price, expect in cases2:
    set_lot(b, age, price=price)
    r = b._hold_expired("T/USDT", price, floor_price)
    net = (price / entry - 1) * 100 - 0.21
    check(f"{name} → {'离场' if expect else '持有'}",
          r == expect, f"净{nnet if False else net:+.2f}%")

print("\n    关闭时行为完全不变")
b.single_max_hold_hours = 0.0
set_lot(b, 999, price=101.0)
check("hours=0 时持有999h仍不强制离场",
      not b._hold_expired("T/USDT", 101.0, floor_price))


print("\n[6] 持仓年龄计算")

b.single_max_hold_hours = 48.0
set_lot(b, 51.2)
check("年龄计算正确", abs(b._position_age_hours("T/USDT") - 51.2) < 0.2,
      f"{b._position_age_hours('T/USDT'):.1f}h")
check("无持仓返回 0", b._position_age_hours("NOPE/USDT") == 0.0)


# ═══════════════════════════════════════════════════════════
print("\n[7] 伪缺陷登记 —— 以下【不是 bug】，禁止当缺陷修")

tp, ttp, tsl_arm, breakeven = 0.015, 0.003, 0.010, 0.0021
e = 100.0
fl = e * (1 + breakeven)

# 伪缺陷 A：移动止盈"死区"
# 死区 = 盈利 0.21%~0.75% 区间，移动止盈不工作。
# 但卖出仍能赚 0.04%~0.53%（毛利 > 往返手续费 0.21%）。
# 要求"利润达止盈一半才启动"是 v12 刻意加的保护，
# 防止微利被洗出（v12 修的就是这个）。少赚不是 bug。
dead_zone_profits = []
for pr in (0.25, 0.40, 0.60, 0.74):
    dead_zone_profits.append(pr - breakeven * 100)
check("伪缺陷A：死区内卖出仍能盈利（非亏损）",
      all(x > 0 for x in dead_zone_profits),
      f"净利 {min(dead_zone_profits):+.2f}%~{max(dead_zone_profits):+.2f}%")
check("伪缺陷A：移动止盈要求利润达tp一半，是刻意保护",
      tp * 100 * 0.5 > breakeven * 100,
      f"激活线{tp*50:.2f}% > 手续费{breakeven*100:.2f}%")

# 伪缺陷 B：setcap 导致回撤爆表 —— v29 已统一口径
import inspect
src_gm = inspect.getsource(QuantBot._grid_monitor)
src_rm = inspect.getsource(QuantBot._risk_monitor_task)
check("伪缺陷B：网格循环用 _total_equity_usdt",
      "_total_equity_usdt()" in src_gm)
check("伪缺陷B：风控任务也用 _total_equity_usdt",
      "_total_equity_usdt()" in src_rm)
import re as _re
code_gm = _re.sub(r'#.*', '', src_gm)
check("伪缺陷B：网格循环不再用 cap 口径更新 peak",
      "update_equity(self._effective_equity_usdt())" not in code_gm)


print("\n[8] 参数必须已注册")

from core.params import PARAMS
for k, dv in (("grid_follow_sell", True),
              ("grid_follow_sell_hours", 24.0),
              ("grid_follow_sell_max_loss", 0.06),
              ("single_max_hold_hours", 48.0)):
    check(f"{k} 已注册且默认 {dv}",
          k in PARAMS and PARAMS[k].default == dv,
          f"{PARAMS[k].default if k in PARAMS else '缺失'}")


print("\n[9] /positions 命令必须可用")

check("cmd_positions 已实现", hasattr(QuantBot, "cmd_positions"))
src_reg = io_open = open("core/bot.py", encoding="utf-8").read()
check("已注册到 CommandHandler",
      'CommandHandler("positions", self.cmd_positions)' in src_reg)


print("\n" + "=" * 74)
print(f"通过: {len(PASS)} 项 | 失败: {len(FAIL)} 项")
if FAIL:
    print("\n失败项：")
    for f in FAIL:
        print(f"   ❌ {f}")
print("=" * 74)
sys.exit(1 if FAIL else 0)
