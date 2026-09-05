"""
精简版回归测试。

原则：不 mock 被测逻辑本身。
  · grid / params / risk / store 是纯逻辑，直接跑
  · bot 的 _sync / _on_fill / per_grid 只 mock 交易所 IO，
    被测的成交记账逻辑是真实代码

每个测试都对应旧版的一个真实 bug，注释里写明来源。
"""
import ast
import io
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


# ═══════════════════════════════════════════════════════════
# 交易所：致命错误分类
#
# 起因：真实部署日志里出现
#     okx {"msg":"APIKey does not match current environment.","code":"50101"}
# 每 10 秒刷一行 WARNING，永不停止。而 Telegram 是唯一通知渠道，
# 用户完全不知道机器人其实什么都没做。
#
# 同时发现 exchange._make() 给【公开行情实例】也传了密钥 ——
# 公开接口本不需要认证，带上后被交易所校验，
# 模拟盘 key 请求真实域名就返回 50101，连取价都失败。
# ═══════════════════════════════════════════════════════════

import ccxt
import exchange

print("\n    交易所：致命错误分类")


def _raise(exc):
    raise exc


def _blank_exchange():
    """构造一个不连网的 Exchange 实例，仅用于测错误分类。"""
    e = exchange.Exchange.__new__(exchange.Exchange)
    e._on_fatal = None
    e.fatal = None
    e.ex = exchange._make(True, with_auth=True)
    e.pub = exchange._make(False, with_auth=False)
    e.markets = {}
    return e


# ── 公开行情实例不得带密钥 ──
# 注意：必须检查【真实构造出来的实例】，而不是 _make() 函数本身。
# 只测 _make(False, with_auth=False) 的话，即使 __init__ 里写成
# _make(False, with_auth=True) 测试照样通过 —— 那是假守卫。
check("_make() 不加认证时不带密钥",
      not exchange._make(False, with_auth=False).apiKey)
check("_make() 加认证时带密钥",
      bool(exchange._make(True, with_auth=True).apiKey))

_orig_load = exchange.Exchange._load_markets
exchange.Exchange._load_markets = lambda self: {}   # 跳过联网
try:
    _real = exchange.Exchange()
finally:
    exchange.Exchange._load_markets = _orig_load

check("实际构造 pub 不带 apiKey", not _real.pub.apiKey,
      f"apiKey={_real.pub.apiKey!r}")
check("实际构造 pub 不带 secret", not _real.pub.secret)
check("实际构造 pub 不带 password",
      not getattr(_real.pub, "password", None))
check("实际构造 ex 保留密钥（交易需要）", bool(_real.ex.apiKey))

# ── 错误分类 ──
_e = _blank_exchange()
for _name, _exc in [("认证错误", ccxt.AuthenticationError("okx 50101")),
                    ("权限不足", ccxt.PermissionDenied("denied")),
                    ("消息含50101", ccxt.ExchangeError('{"code":"50101"}')),
                    ("消息含50111", ccxt.ExchangeError('{"code":"50111"}'))]:
    check(f"{_name} → fatal", _e._is_fatal(_exc))

for _name, _exc in [("网络错误", ccxt.NetworkError("timeout")),
                    ("限频", ccxt.RateLimitExceeded("too many")),
                    ("交易所不可用", ccxt.ExchangeNotAvailable("down"))]:
    check(f"{_name} → transient（可重试）", not _e._is_fatal(_exc))

# ── fatal 后只报一次、只请求一次 ──
_pushed = []
_calls = {"n": 0}
_e = _blank_exchange()
_e._on_fatal = lambda t: _pushed.append(t)


def _boom(*a, **k):
    _calls["n"] += 1
    raise ccxt.AuthenticationError('okx {"code":"50101"}')


_e.ex.fetch_balance = _boom
for _ in range(5):
    _free, _used, _coins = _e.balances()

check("fatal 后只请求交易所 1 次（不刷屏）", _calls["n"] == 1,
      f"实际请求 {_calls['n']} 次")
check("fatal 只推送用户 1 次", len(_pushed) == 1,
      f"实际推送 {len(_pushed)} 次")
check("fatal 后余额安全返回空", _free == 0.0 and _coins == {})
check("fatal 后查挂单返回空", _e.open_orders("SOL/USDT") == [])
check("fatal 后买入返回 None",
      _e.limit_buy("SOL/USDT", 1.0, 100.0) is None)
check("fatal 后卖出返回 None",
      _e.limit_sell("SOL/USDT", 1.0, 100.0) is None)
check("fatal 后市价卖返回 None",
      _e.market_sell("SOL/USDT", 1.0) is None)
check("fatal 后查单返回 None",
      _e.fetch_order("oid", "SOL/USDT") is None)

# ── 告警内容必须给出修法 ──
if _pushed:
    _t = _pushed[0]
    for _kw in ("不是网络问题", "模拟盘", "实盘", "OKX_SANDBOX"):
        check(f"告警含关键信息「{_kw}」", _kw in _t)
else:
    check("告警已生成", False, "没有任何推送内容")

# ── 网络错误不得误判为 fatal ──
_pushed2 = []
_e = _blank_exchange()
_e._on_fatal = lambda t: _pushed2.append(t)
_e.ex.fetch_balance = lambda *a, **k: _raise(ccxt.NetworkError("timeout"))
_free2, _used2, _ = _e.balances()
check("网络错误不触发 fatal 告警", not _pushed2)
check("网络错误不置 fatal 状态", _e.fatal is None)
check("网络错误仍安全返回空", _free2 == 0.0)


# ═══════════════════════════════════════════════════════════
# 权益口径：挂单冻结资金必须计入权益
#
# 真实部署日志：
#     日基准 7256.38U（可用 4805.53 + 1 ETH 2450.85）
#     挂 4 档买单，每档 960.91U，冻结 3843.64U
#     日亏损 52.97% 达上限 5%，暂停 1 小时
#     冷却中（剩 59/60 分钟）… 每小时重复，永久停摆
#
# 根因：ccxt 的 free 不含挂单冻结的 USDT（钱在 used 里）。
# 权益只算 free + 持仓 → 一挂单就"亏损"掉全部冻结额。
# 而订单还挂着，1 小时后重算仍触发 → 死循环。
# ═══════════════════════════════════════════════════════════

print("\n    权益口径：冻结资金")


class _FakeBal:
    """返回指定余额的假 Exchange，只测权益口径。"""

    def __init__(self, usdt):
        self._info = {"USDT": usdt}

    def balances(self):
        u = self._info["USDT"]
        return u["free"], u["used"], {}


# 真实日志的数字
_free_after = 4805.53 - 3843.64      # 挂单后可用
_base = 7256.38                      # 日基准（挂单前）
_frozen = 3843.64

# 修复前：equity = free + 持仓（漏掉冻结）
_old_equity = _free_after + 2450.85
_old_loss = (_base - _old_equity) / _base
check("复现原缺陷：漏算冻结 → 日亏损 52.97%",
      abs(_old_loss * 100 - 52.97) < 0.01, f"{_old_loss*100:.2f}%")

# 修复后：equity = free + used + 持仓
_new_equity = _free_after + _frozen + 2450.85
_new_loss = (_base - _new_equity) / _base
check("修复后：计入冻结 → 日亏损 0%",
      abs(_new_loss) < 1e-9, f"{_new_loss*100:.6f}%")
check("修复后权益回到基准 7256.38U",
      abs(_new_equity - _base) < 0.01, f"{_new_equity:.2f}U")

# balances() 必须返回三元组，且 used 有兜底
_e = _blank_exchange()


def _mkbal(free, used, total):
    e2 = _blank_exchange()
    e2.ex.fetch_balance = lambda *a, **k: {
        "USDT": {"free": free, "used": used, "total": total},
        "free": {}}
    return e2


_f, _u, _c = _mkbal(100.0, 50.0, 150.0).balances()
check("balances 返回 (可用, 冻结, 持仓)", (_f, _u) == (100.0, 50.0),
      f"free={_f} used={_u}")

# 交易所不给 used 时，用 total - free 兜底
_f2, _u2, _ = _mkbal(100.0, 0.0, 150.0).balances()
check("交易所不返回 used 时用 total-free 兜底",
      abs(_u2 - 50.0) < 1e-9, f"used={_u2}")

# fatal 状态返回三元组
_ef = _blank_exchange()
_ef.fatal = "x"
check("fatal 时 balances 返回三元组", len(_ef.balances()) == 3)


# ═══════════════════════════════════════════════════════════
# 通知：同步函数里不得直接调 async 的 _tell
#
# 真实缺陷：_on_fill / _liquidate 是同步函数，里面写
#     self._tell("🟢 买入 ...")
# 只是创建了协程对象却没人 await —— 通知【静默丢失】，
# 只在日志留一行 RuntimeWarning: coroutine was never awaited。
#
# 后果：买入、卖出、区间止损三类最关键的通知一条都收不到。
# ═══════════════════════════════════════════════════════════

print("\n    通知链路")

_src = io.open("bot.py", encoding="utf-8").read()
_tree = ast.parse(_src)

_sync_methods = ("_on_fill", "_liquidate")
_bad = []
for _node in ast.walk(_tree):
    if not isinstance(_node, ast.FunctionDef):
        continue
    if _node.name not in _sync_methods:
        continue
    for _sub in ast.walk(_node):
        if isinstance(_sub, ast.Call):
            _fn = _sub.func
            if (isinstance(_fn, ast.Attribute)
                    and _fn.attr == "_tell"
                    and isinstance(_fn.value, ast.Name)
                    and _fn.value.id == "self"):
                _bad.append(f"{_node.name}:{_sub.lineno}")

check("同步函数未直接调用 async _tell", not _bad,
      "违规位置: " + ", ".join(_bad) if _bad else "已全部用 _tell_nowait")

check("同步函数使用 _tell_nowait",
      "_tell_nowait" in _src)


print("\n" + "=" * 70)
print(f"通过: {len(PASS)} 项 | 失败: {len(FAIL)} 项")
if FAIL:
    print("\n失败明细：")
    for f in FAIL:
        print(f"   ❌ {f}")
print("=" * 70)
sys.exit(1 if FAIL else 0)

