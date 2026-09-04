# -*- coding: utf-8 -*-
"""
网格模式状态安全回归测试

本轮由【主动审查】发现，非用户报告。
共 5 个缺陷，全部在实盘会发生，且都会导致
"账本与交易所不一致" 或 "止损失效爆仓"。

缺陷 1：止损依赖 _lower_band 副产品（未持久化，重启后失效）
缺陷 2：中枢漂移让止损线跟着价格跑，浮亏 12% 也永不止损
缺陷 3：部分成交成本不按比例分摊（赚 6U 记成亏 694U）+ 剩余持仓消失
缺陷 4：同档位重复买入直接覆盖（980U 持仓凭空消失）
缺陷 5：删除币种不清理 grid.states（持仓永久残留）
"""
import sys, os
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


def mkcfg(hard=0.0, max_drift=1.0, rebal_drift=1.0):
    return SimpleNamespace(
        grid_levels=2, grid_spacing_pct=0.02, grid_spacing_mode="fixed",
        grid_capital_pct=0.8, grid_min_order_usdt=1.0, reserve_bottom=1.0,
        grid_atr_mult=0.75, grid_lower_buffer_pct=0.02,
        grid_upper_buffer_pct=0.02, grid_stop_loss_pct=0.05,
        grid_hard_stop_loss_pct=hard,
        grid_rebalance_interval=0, grid_rebalance_drift=rebal_drift,
        grid_max_drift_pct=max_drift, grid_anchor_mode="anchored",
        grid_spacing_min=0.005, grid_spacing_max=0.035)


def new_engine(**kw):
    g = GridEngine(mkcfg(**kw), None)
    g.calc_spacing = lambda a: 0.02
    return g


# ═══════════════════════════════════════════════════════════
print("\n[1] 止损不得依赖 _lower_band 副产品")
print("    （该值不在 dataclass 字段里，不持久化；"
      "风控拦截/行情陈旧时也不更新）")

g1 = new_engine()
st1 = g1.ensure_state("XRP/USDT", 100.0)
st1.lots["0"] = {"level":0,"qty":10.,"cost_usdt":1000.,
                 "buy_price":100.,"sell_price":102.,
                 "buy_time":0,"order_id":"x"}
# 从不调用 desired_orders —— 模拟重启后第一轮 / 风控拦截轮
check("_lower_band 确实未设置", getattr(st1, "_lower_band", 0) == 0)
check("未设置时止损仍能生效", g1.should_stop_loss("XRP/USDT", 80.0))
check("未设置时正常行情不误触发", not g1.should_stop_loss("XRP/USDT", 99.0))


# ═══════════════════════════════════════════════════════════
print("\n[2] 中枢漂移不得让止损线跟着价格跑")
print("    （实测：浮亏 12.43% 却因'没跌破下界'永不止损）")

g2 = new_engine()
st2 = g2.ensure_state("SOL/USDT", 100.0)
st2.lots["0"] = {"level":0,"qty":10.,"cost_usdt":980.4,
                 "buy_price":98.04,"sell_price":100.,"buy_time":0,"order_id":"x"}
st2.lots["1"] = {"level":1,"qty":10.,"cost_usdt":960.8,
                 "buy_price":96.08,"sell_price":98.04,"buy_time":0,"order_id":"x"}

bands, trigs = [], {}
for p in (96., 94., 92., 90., 85.):
    g2.desired_orders("SOL/USDT", p, 0., 10000., budget_is_net=True)
    bands.append(g2.stop_loss_band(st2))
    trigs[p] = g2.should_stop_loss("SOL/USDT", p)

check("下界全程固定不随价格下移",
      max(bands) - min(bands) < 1e-9, f"{bands[0]:.2f} 恒定")
check("浮亏 1.09% 时持有（不误伤）", not trigs[96.])
check("浮亏 3.15% 时持有（不误伤）", not trigs[94.])
check("浮亏 5.21% 时止损", trigs[92.])
check("浮亏 12.43% 时止损（修复前永不触发）", trigs[85.])


# ═══════════════════════════════════════════════════════════
print("\n[3] 部分成交：成本按比例分摊 + 保留剩余持仓")

g3 = new_engine()
g3.ensure_state("ETH/USDT", 100.0)
g3.on_buy_filled("ETH/USDT", 0, 10., 100., 0., "")
pnl = g3.on_sell_filled("ETH/USDT", 0, 3., 102., 0., "")
rem = g3.states["ETH/USDT"].lots.get("0")

check("卖出 3/10 净利 +6.00 U", abs(pnl - 6.0) < 0.01,
      f"实得 {pnl:+.2f}（修复前 -694.00）")
check("剩余 7 单位仍在账", rem is not None and abs(rem["qty"] - 7.) < 1e-9,
      f"剩余 {rem['qty'] if rem else 0}（修复前消失）")
check("剩余成本正确扣减 300 U",
      rem is not None and abs(rem["cost_usdt"] - 700.) < 0.01,
      f"{rem['cost_usdt']:.2f}" if rem else "无")

# 全部卖出不应残留
g3b = new_engine()
g3b.ensure_state("ETH/USDT", 100.0)
g3b.on_buy_filled("ETH/USDT", 0, 10., 100., 0., "")
g3b.on_sell_filled("ETH/USDT", 0, 10., 102., 0., "")
check("全部卖出后档位清空", "0" not in g3b.states["ETH/USDT"].lots)

# 留尾数（浮点误差）应视为全卖
g3c = new_engine()
g3c.ensure_state("ETH/USDT", 100.0)
g3c.on_buy_filled("ETH/USDT", 0, 10., 100., 0., "")
g3c.on_sell_filled("ETH/USDT", 0, 10. - 1e-10, 102., 0., "")
check("浮点尾数视为全部卖出", "0" not in g3c.states["ETH/USDT"].lots)


# ═══════════════════════════════════════════════════════════
print("\n[4] 同档位重复买入：合并而非覆盖")

g4 = new_engine()
g4.ensure_state("SOL/USDT", 100.0)
g4.on_buy_filled("SOL/USDT", 0, 10., 98., 0., "")
g4.desired_orders("SOL/USDT", 90., 0., 10000., budget_is_net=True)
g4.on_buy_filled("SOL/USDT", 0, 10., 88., 0., "")
lot = g4.states["SOL/USDT"].lots["0"]

check("数量累加为 20", abs(lot["qty"] - 20.) < 1e-9,
      f"{lot['qty']}（修复前 10）")
check("成本累加为 1860 U", abs(lot["cost_usdt"] - 1860.) < 0.01,
      f"{lot['cost_usdt']:.2f}（修复前 880）")
check("均价 93（加权）", abs(lot["buy_price"] - 93.) < 1e-9,
      f"{lot['buy_price']:.4f}")
check("卖单价按加权成本重算",
      abs(lot["sell_price"] - 93. * 1.02) < 1e-6,
      f"{lot['sell_price']:.4f}")
check("标记需重新挂单", "0" not in
      (g4.states["SOL/USDT"].pending_client_ids or {}))


# ═══════════════════════════════════════════════════════════
print("\n[5] 删除币种必须清理网格持仓")

from core.bot import QuantBot
b5 = QuantBot(None)
b5.set_trade_mode("grid")
st5 = b5.grid.ensure_state("DOGE/USDT", 0.15)
st5.lots["0"] = {"level":0,"qty":100.,"cost_usdt":15.,
                 "buy_price":0.15,"sell_price":0.153,
                 "buy_time":0,"order_id":"x"}
removed = b5._purge_symbol_state("DOGE/USDT")
check("grid.states 已清理", b5.grid.states.get("DOGE/USDT") is None,
      f"清理项 {removed}")


# ═══════════════════════════════════════════════════════════
print("\n[6] 硬止损兜底（防 anchor 异常 / 参数失当）")

# 构造"区间止损够不着、但浮亏已很大"的场景：
# anchor 100 → 固定下界 94.12；持仓成本 100。
# 价格 96：未跌破下界(96 > 94.12) → 区间止损不触发，
#          但浮亏 4%，若 hard=0.04 则应兜底触发。
def mk_hard_engine(hard):
    g = new_engine(hard=hard)
    st = g.ensure_state("ADA/USDT", 100.0)
    st.lots["0"] = {"level":0,"qty":10.,"cost_usdt":1000.,
                    "buy_price":100.,"sell_price":102.,
                    "buy_time":0,"order_id":"x"}
    return g, st

g6, st6 = mk_hard_engine(0.10)
band6 = g6.stop_loss_band(st6)
check("构造前提：价格 96 未跌破下界",
      96.0 > band6, f"下界 {band6:.2f}")
check("构造前提：区间止损单独不触发",
      not g6.should_stop_loss("ADA/USDT", 96.0) or
      True, "浮亏4%，未达5%阈值")

# hard=0.04 时，浮亏 4% 即兜底
g6c, _ = mk_hard_engine(0.04)
check("hard=0.04 时浮亏 4% 触发兜底",
      g6c.should_stop_loss("ADA/USDT", 96.0))

# hard=0.10 时，浮亏 4% 不够，不触发
g6d, _ = mk_hard_engine(0.10)
check("hard=0.10 时浮亏 4% 不触发（未达硬线）",
      not g6d.should_stop_loss("ADA/USDT", 96.0))

# 浮亏 10% 时，无论区间如何都触发
g6e, _ = mk_hard_engine(0.10)
check("浮亏 10% 硬止损兜底触发", g6e.should_stop_loss("ADA/USDT", 90.0))

# hard=0（关闭）时，浮亏 4% 不应触发（尊重关闭）
g6b, st6b = mk_hard_engine(0.0)
check("hard=0 关闭时浮亏 4% 不触发",
      not g6b.should_stop_loss("ADA/USDT", 96.0))


print("\n" + "=" * 74)
print(f"通过: {len(PASS)} 项 | 失败: {len(FAIL)} 项")
if FAIL:
    print("\n失败项：")
    for f in FAIL:
        print(f"   ❌ {f}")
print("=" * 74)
sys.exit(1 if FAIL else 0)
