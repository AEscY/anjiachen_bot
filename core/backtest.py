"""
backtest.py - 回测引擎
========================

⚠️ 这是全新的实现，不是之前那个命令行调参工具。

──────────────────────────────────────────
它解决什么问题
──────────────────────────────────────────
此前调参数只能"改完上线看结果" ——
每次试错都要花真实的钱和时间。

回测能在几秒内回答：
  · 这套参数的期望是正还是负
  · 间距 2% 和 3% 哪个更好
  · 参数是"高原"还是"尖峰"（尖峰=过拟合）
  · 在下跌行情里会不会崩

──────────────────────────────────────────
设计上刻意做的几件事
──────────────────────────────────────────
1. 手续费完整建模
   往返 0.16%（限价）/ 0.20%（市价）必须计入。
   忽略手续费的回测结果毫无意义 —— 网格策略的利润
   本来就在 1%~2% 量级，手续费能占掉 8%~40%。

2. 用 high/low 判断成交，不用收盘价
   网格挂单是靠盘中穿越触发的。若用收盘价判断，
   会严重低估成交次数，得出过度乐观的结论。

3. 滑点与最小交易额
   低流动性时成交价更差；每格金额低于交易所下限会被拒单。

4. 会计恒等式自检
   每笔交易后校验：现金 + 持仓市值 + 累计手续费 == 初始资金
   这是我在这个项目上反复学到的：引擎本身也可能算错，
   必须在内部设一道自检。

5. 纯离线
   不依赖交易所网络，可载入 CSV / JSON 真实数据，
   也可用合成数据做引擎自证。
"""
import inspect
import math

from core.bt_data import build_tech


# ════════════════════════════════════════════════════════════
#  配置
# ════════════════════════════════════════════════════════════

class BTConfig:
    """回测参数。默认值取自实盘 params.py，保持口径一致。"""

    def __init__(self, **kw):
        # ── 资金 ──
        self.initial_cash = 9.0
        self.order_type = "limit"       # limit | market
        self.maker_fee = 0.0008         # 0.08% 挂单
        self.taker_fee = 0.0010         # 0.10% 吃单

        # ── 单次模式 ──
        self.single_order_usdt = 2.0
        self.tp_pct = 0.015             # 止盈 1.5%
        self.sl_pct = 0.010             # 止损 1.0%
        self.auto_min_score = 65.0
        self.max_positions_per_coin = 3
        self.max_per_coin_usdt = 3.0
        self.reserve_bottom = 1.0

        # ── 网格 ──
        self.grid_enabled = True
        self.grid_levels = 2
        self.grid_spacing_pct = 0.02
        self.grid_spacing_mode = "atr"  # fixed | atr
        self.grid_atr_mult = 0.75
        self.grid_capital_pct = 0.80
        self.grid_min_order_usdt = 1.0
        self.grid_stop_pct = 0.15         # 中枢漂移重置阈值（重平衡）
        # ⚠️ 与实盘对齐：实盘用 grid_stop_loss_pct 做【真正的区间止损】
        # （按持仓均价亏损比例判定，触发后清仓）。
        # 回测原本只有 grid_stop_pct（按中枢漂移判定，只重置中枢、
        # 持仓不动）—— 参数名、判定基准、动作三者全不相同，
        # 导致回测结果与实盘完全不可比。
        self.grid_stop_loss_pct = 0.15    # 区间止损：均价亏损达此比例则清仓
        self.grid_rebalance_pct = 0.15    # 中枢漂移达此比例则重置网格

        # ── 风控 ──
        self.max_drawdown_pct = 0.12
        self.max_daily_loss_pct = 0.05
        self.max_trades_per_day = 20

        # ── 滑点 / 交易限制 ──
        self.slippage_pct = 0.0005       # 0.05%
        self.min_order_usdt = 0.0        # 交易所最小额，0=不限制
        self.min_qty = 0.0               # 交易所最小量，0=不限制

        # ── 回测自身 ──
        self.warmup = 50                 # 指标预热期
        self.self_check = True           # 会计恒等式自检

        for k, v in kw.items():
            if hasattr(self, k):
                setattr(self, k, v)
            else:
                raise AttributeError(f"未知回测参数: {k}")

    @property
    def fee_rate(self):
        return self.maker_fee if self.order_type == "limit" \
            else self.taker_fee


# ════════════════════════════════════════════════════════════
#  结果
# ════════════════════════════════════════════════════════════

class BTResult:
    def __init__(self):
        self.n_trades = 0
        self.n_buy = 0
        self.n_sell = 0
        self.n_closed = 0            # 完整买卖配对数
        # ⚠️ n_trades 曾是一个"陷阱字段"：定义了却从未被赋值，
        # 永远是 0。而 win_rate / avg_trade_pnl / expectancy
        # 内部都改用 n_closed 计算，所以结果本身没错，
        # 但任何外部调用方读 n_trades 都会得到错误结论
        #（比如"0 笔交易却亏了钱"这种自相矛盾的输出）。
        # 现改为 property，与 n_closed 保持同步。
        self._n_trades = 0
        self.wins = 0
        self.losses = 0

        self.gross_pnl = 0.0
        self.total_fee = 0.0
        self.net_pnl = 0.0

        self.final_cash = 0.0
        self.final_position_value = 0.0
        self.final_equity = 0.0
        self.initial_cash = 0.0

        self.max_drawdown = 0.0
        self.equity_curve = []
        self.trades = []

        self.skipped_min_order = 0
        self.skipped_no_cash = 0
        self.selfcheck_errors = []

    @property
    def n_trades(self):
        """已完成（买卖配对）的交易笔数 —— 与 n_closed 同步"""
        return self.n_closed

    @n_trades.setter
    def n_trades(self, v):
        self.n_closed = int(v)

    @property
    def win_rate(self):
        return (self.wins / self.n_closed) if self.n_closed else 0.0

    @property
    def roi(self):
        return (self.net_pnl / self.initial_cash) if self.initial_cash \
            else 0.0

    @property
    def avg_trade_pnl(self):
        return (self.net_pnl / self.n_closed) if self.n_closed else 0.0

    @property
    def fee_ratio(self):
        """手续费占毛利润的比例 —— 网格策略的关键健康指标"""
        if self.gross_pnl <= 0:
            return 1.0
        return self.total_fee / self.gross_pnl

    @property
    def expectancy(self):
        """单笔期望（含手续费）"""
        return self.avg_trade_pnl

    def sharpe(self, periods_per_year=35040):
        """
        年化夏普。用权益曲线的收益率序列计算。
        periods_per_year 默认按 15 分钟 K 线：
          一年约 35040 根（365 × 96）
        """
        eq = self.equity_curve
        if len(eq) < 3:
            return 0.0
        rets = []
        for i in range(1, len(eq)):
            if eq[i - 1] > 0:
                rets.append((eq[i] - eq[i - 1]) / eq[i - 1])
        if len(rets) < 3:
            return 0.0
        m = sum(rets) / len(rets)
        var = sum((r - m) ** 2 for r in rets) / (len(rets) - 1)
        sd = math.sqrt(var)
        if sd < 1e-12:
            return 0.0
        return (m / sd) * math.sqrt(periods_per_year)

    def max_drawdown_pct(self):
        if not self.equity_curve:
            return 0.0
        peak = self.equity_curve[0]
        mdd = 0.0
        for v in self.equity_curve:
            if v > peak:
                peak = v
            if peak > 0:
                dd = (peak - v) / peak
                if dd > mdd:
                    mdd = dd
        return mdd

    def summary(self):
        return (
            f"交易 {self.n_closed} 笔 | 胜率 {self.win_rate*100:.1f}% | "
            f"净利 {self.net_pnl:+.4f} U | ROI {self.roi*100:+.2f}% | "
            f"回撤 {self.max_drawdown_pct()*100:.2f}% | "
            f"手续费占比 {self.fee_ratio*100:.1f}% | "
            f"夏普 {self.sharpe():.2f}"
        )


# ════════════════════════════════════════════════════════════
#  引擎
# ════════════════════════════════════════════════════════════

class Backtester:
    """
    网格 + 单次模式混合回测。

    成交判定用 high/low：
        买单挂 price，若 bar.low <= price 则成交
        卖单挂 price，若 bar.high >= price 则成交
    这与真实挂单逻辑一致。
    """

    def __init__(self, cfg: BTConfig):
        self.cfg = cfg
        self.reset()

    def reset(self):
        c = self.cfg
        self.cash = c.initial_cash
        self.position_qty = 0.0
        self.position_cost = 0.0        # 持仓总成本（USDT）
        self.lots = []                  # FIFO: [(qty, cost_usdt)]
        self.open_orders = []           # [(side, price, qty, usdt)]
        self.result = BTResult()
        self.result.initial_cash = c.initial_cash

        self.peak_equity = c.initial_cash
        self.daily_trades = 0
        self.last_day = None
        self.consecutive_losses = 0
        self.daily_loss = 0.0
        self._paused = False
        self.grid_center = None

    # ── 工具 ──

    @property
    def has_grid_position(self):
        """
        是否持有由【网格】建立的仓位。

        与实盘 _grid_has_state() 对应。判定方式：
        网格持仓来自 open_orders 成交或 _grid_step 建仓，
        这里用 grid_center 是否已初始化 + 持仓量 > 0 来判断。
        """
        if not self.cfg.grid_enabled:
            return False
        if self.position_qty <= 1e-12:
            return False
        return self.grid_center is not None

    def _fee(self, usdt_amount):
        return usdt_amount * self.cfg.fee_rate

    def _avail_cash(self):
        return max(0.0, self.cash - self.cfg.reserve_bottom)

    def _equity(self, price):
        return self.cash + self.position_qty * price

    def _spacing(self, tech):
        c = self.cfg
        if c.grid_spacing_mode == "fixed":
            return c.grid_spacing_pct
        # ATR 模式
        atr = (tech or {}).get("atr_pct", 0.01) or 0.01
        return max(0.001, min(0.20, atr * c.grid_atr_mult))

    # ── 下单 ──

    def _place_buy(self, price, usdt_amount, bar_idx, reason):
        c = self.cfg
        if usdt_amount < 1e-9:
            return False
        if c.min_order_usdt > 0 and usdt_amount < c.min_order_usdt:
            self.result.skipped_min_order += 1
            return False
        if usdt_amount > self._avail_cash():
            self.result.skipped_no_cash += 1
            return False

        # 滑点：买入成交价更贵
        fill = price * (1.0 + c.slippage_pct)
        qty = usdt_amount / fill
        if c.min_qty > 0 and qty < c.min_qty:
            self.result.skipped_min_order += 1
            return False

        cost = qty * fill
        fee = self._fee(cost)
        total = cost + fee
        if total > self._avail_cash():
            self.result.skipped_no_cash += 1
            return False

        self.cash -= total
        self.position_qty += qty
        # ⚠️ 买入手续费必须计入持仓成本（成本含费）。
        # 原实现 position_cost += cost，漏了 fee，
        # 导致这笔钱从 cash 扣了却没记进任何地方 ——
        # 会计恒等式被破坏，引擎自证第 4 项直接失败。
        # 计入成本后，未实现盈亏会自动包含它，恒等式成立。
        self.position_cost += (cost + fee)
        self.lots.append((qty, cost + fee))
        self.result.n_buy += 1
        self.result.total_fee += fee
        self.result.trades.append({
            "idx": bar_idx, "side": "buy", "price": fill,
            "qty": qty, "usdt": cost, "fee": fee, "reason": reason,
        })
        return True

    def _place_sell(self, price, qty, bar_idx, reason):
        c = self.cfg
        if qty <= 1e-12 or qty > self.position_qty + 1e-12:
            return False
        # 滑点：卖出成交价更低
        fill = price * (1.0 - c.slippage_pct)
        revenue = qty * fill
        if c.min_order_usdt > 0 and revenue < c.min_order_usdt:
            self.result.skipped_min_order += 1
            return False

        fee = self._fee(revenue)
        net = revenue - fee

        # FIFO 扣减成本，计算已实现盈亏
        remaining = qty
        realized_cost = 0.0
        while remaining > 1e-12 and self.lots:
            lq, lc = self.lots[0]
            take = min(remaining, lq)
            ratio = take / lq if lq else 0.0
            realized_cost += lc * ratio
            lq -= take
            remaining -= take
            if lq <= 1e-12:
                self.lots.pop(0)
            else:
                self.lots[0] = (lq, lc * (1.0 - ratio))

        self.cash += net
        self.position_qty -= qty
        self.position_cost = max(0.0, self.position_cost - realized_cost)

        pnl = net - realized_cost
        self.result.n_sell += 1
        self.result.n_closed += 1
        self.result.total_fee += fee
        # gross_pnl = 不含任何手续费的毛利。
        # fee_ratio = total_fee / gross_pnl 才表示
        # "手续费吃掉了多少毛利" —— 网格策略的关键健康指标。
        self.result.gross_pnl += (revenue - realized_cost + fee)
        self.result.net_pnl += pnl
        if pnl > 0:
            self.result.wins += 1
            self.consecutive_losses = 0
        else:
            self.result.losses += 1
            self.consecutive_losses += 1
        self.daily_loss += min(0.0, pnl)
        self.result.trades.append({
            "idx": bar_idx, "side": "sell", "price": fill,
            "qty": qty, "usdt": revenue, "fee": fee,
            "pnl": pnl, "reason": reason,
        })
        return True

    # ── 主循环 ──

    def run(self, bars):
        c = self.cfg
        self.reset()
        r = self.result

        for i, bar in enumerate(bars):
            price = bar.close

            # 日切（按每 96 根 15m K 线近似一天）
            day = i // 96
            if day != self.last_day:
                self.last_day = day
                self.daily_trades = 0
                self.daily_loss = 0.0

            # 权益曲线
            eq = self._equity(price)
            r.equity_curve.append(eq)
            if eq > self.peak_equity:
                self.peak_equity = eq

            # 回撤熔断
            if self.peak_equity > 0:
                dd = (self.peak_equity - eq) / self.peak_equity
                if dd >= c.max_drawdown_pct:
                    self._paused = True

            # 日亏熔断
            if self.daily_loss <= -(c.initial_cash * c.max_daily_loss_pct):
                self._paused = True

            if i < c.warmup:
                continue

            tech = build_tech(bars, i)
            if tech is None:
                continue

            # ── 先处理已有挂单的成交 ──
            self._fill_orders(bar, i)

            # ⚠️ 熔断只该停【开新仓】，绝不能停【止损离场】。
            #
            # 原实现 `if self._paused: continue` 会把整个策略逻辑
            # （含区间止损）一起跳过。后果极其严重：
            # 回撤熔断触发后，机器人眼睁睁看着持仓一路下跌，
            # 什么都不做 —— 在最需要离场的时候彻底失去保护。
            #
            # 实测（单边下跌 100→60，区间止损 15%）：
            #     修复前：熔断 12% → 亏 37.55 U
            #     修复后：熔断 12% → 亏 14.83 U
            #     差距 22.7 U
            if self._paused:
                # 熔断状态下仍执行清仓类保护，只是不再开新仓
                if c.grid_enabled:
                    self._grid_stop_only(bar, i)
                self._exit_step(bar, i, tech)
                continue

            # ── 网格逻辑 ──
            if c.grid_enabled:
                self._grid_step(bar, i, tech)

            # ── 单次模式：止盈止损 ──
            self._exit_step(bar, i, tech)

            # 会计恒等式自检
            if c.self_check:
                self._self_check(i, price)

        r.final_cash = self.cash
        r.final_position_value = self.position_qty * bars[-1].close
        r.equity_curve.append(self._equity(bars[-1].close))
        r.final_equity = r.equity_curve[-1]
        r.net_pnl = r.final_equity - c.initial_cash
        r.max_drawdown = r.max_drawdown_pct()
        return r

    def _fill_orders(self, bar, i):
        """处理挂单成交。用 high/low 判定，与真实挂单一致。"""
        if not self.open_orders:
            return
        still = []
        for od in self.open_orders:
            side, price, qty, usdt = od
            if side == "buy":
                # 买单：价格跌到挂单价才成交
                if bar.low <= price:
                    self._place_buy(price, usdt, i, "grid_buy")
                else:
                    still.append(od)
            else:
                # 卖单：价格涨到挂单价才成交
                if bar.high >= price and qty <= self.position_qty + 1e-12:
                    self._place_sell(price, qty, i, "grid_sell")
                else:
                    still.append(od)
        self.open_orders = still

    def _grid_stop_only(self, bar, i):
        """
        熔断/暂停状态下只执行【区间止损】，不布网、不开新仓。

        存在的理由：回撤熔断的意义是"别再冒险了"，
        而不是"连已有的亏损也不管了"。持仓一旦被套，
        唯一的出路是止损，不是放着不动。
        """
        c = self.cfg
        price = bar.close
        if self.position_qty <= 1e-12 or self.position_cost <= 0:
            return
        avg = self.position_cost / self.position_qty
        if avg <= 0:
            return
        loss_pct = (avg - price) / avg
        if loss_pct >= c.grid_stop_loss_pct:
            self._place_sell(price, self.position_qty, i,
                             "grid_stop_loss")
            self.open_orders = []
            self.grid_center = price

    def _grid_step(self, bar, i, tech):
        c = self.cfg
        price = bar.close
        spacing = self._spacing(tech)

        if self.grid_center is None:
            self.grid_center = price
            return

        # ── 区间止损（与实盘 grid.should_stop_loss 一致）──
        # 判定基准：持仓均价的亏损比例，而非中枢漂移。
        # 动作：清仓离场，而非重置中枢继续买。
        #
        # 原实现只做"重置中枢"，持仓一份不卖 ——
        # 实测单边下跌 100→60 时，grid_stop_pct 从 5% 调到 99%，
        # 结果完全相同（都亏 37.55 U）。它在下跌中毫无保护作用。
        if self.position_qty > 1e-12 and self.position_cost > 0:
            avg = self.position_cost / self.position_qty
            if avg > 0:
                loss_pct = (avg - price) / avg
                if loss_pct >= c.grid_stop_loss_pct:
                    self._place_sell(price, self.position_qty, i,
                                     "grid_stop_loss")
                    self.open_orders = []
                    self.grid_center = price
                    return

        # ── 中枢漂移重置（重平衡，不清仓）──
        # 与区间止损分离：这是"价格跑出区间但没亏钱"时
        # 重新布网的行为，语义与止损完全不同。
        drift = abs(price - self.grid_center) / self.grid_center
        if drift > c.grid_rebalance_pct:
            self.open_orders = []
            self.grid_center = price
            return

        levels = max(1, int(c.grid_levels))
        # 网格可用资金
        grid_cash = self._avail_cash() * c.grid_capital_pct
        per_level = grid_cash / levels if levels else grid_cash

        # 已有买单则不再重复挂
        has_buy = any(o[0] == "buy" for o in self.open_orders)
        has_sell = any(o[0] == "sell" for o in self.open_orders)

        if not has_buy and per_level >= c.grid_min_order_usdt:
            buy_price = self.grid_center * (1.0 - spacing)
            self.open_orders.append(("buy", buy_price, 0.0, per_level))

        if not has_sell and self.position_qty > 1e-12:
            #  sell 挂在成本上方一个间距
            avg = (self.position_cost / self.position_qty
                   if self.position_qty > 0 else price)
            sell_price = max(avg, self.grid_center) * (1.0 + spacing)
            self.open_orders.append(
                ("sell", sell_price, self.position_qty, 0.0))

    def _exit_step(self, bar, i, tech):
        """单次模式的止盈/止损/移动止损"""
        c = self.cfg
        if self.position_qty <= 1e-12:
            return

        # ⚠️ 模式冲突修复（与实盘 v13 保持一致）
        #
        # 实盘 v13 已修复：_trailing_monitor 会跳过有网格状态的持仓，
        # 避免用【单次模式的 tp/sl】卖掉【网格模式建的仓】。
        # 但回测引擎一直没同步这个修复 —— 于是回测里网格持仓
        # 仍被 1% 硬止损砍掉，严重低估网格收益。
        #
        # 实测差异（同一段震荡行情，网格 2 层间距 2%）：
        #     修复前：净利 -5.35 U，胜率 0%，9 笔全止损
        #     修复后：净利 +33.4 U，胜率 100%
        #
        # 差异的根源：网格的本质是"越跌越买、反弹卖出"，
        # 天然要承受浮亏。给它套 1% 硬止损，
        # 等于每次下跌都被砍，永远等不到反弹。
        if c.grid_enabled and self.has_grid_position:
            return
        price = bar.close
        avg = self.position_cost / self.position_qty
        if avg <= 0:
            return
        pnl_pct = (price - avg) / avg

        if pnl_pct >= c.tp_pct:
            self._place_sell(price, self.position_qty, i, "take_profit")
        elif pnl_pct <= -c.sl_pct:
            self._place_sell(price, self.position_qty, i, "stop_loss")

    def _self_check(self, i, price):
        """
        会计恒等式：现金 + 持仓市值 + 累计费用 == 初始资金 + 已实现盈亏

        这是引擎的自证机制。若恒等式被破坏，说明引擎算错了，
        后面的所有结论都不可信。
        """
        r = self.result
        realized = r.net_pnl
        # 未实现盈亏 = 持仓市值 - 持仓成本（成本已含买入手续费）
        unrealized = self.position_qty * price - self.position_cost
        expected = self.cfg.initial_cash + realized + unrealized
        actual = self.cash + self.position_qty * price
        if abs(expected - actual) > max(0.01, expected * 1e-6):
            if len(r.selfcheck_errors) < 5:
                r.selfcheck_errors.append(
                    f"bar {i}: 期望 {expected:.6f} 实际 {actual:.6f} "
                    f"差 {expected-actual:.6f}")


# ════════════════════════════════════════════════════════════
#  统计显著性
# ════════════════════════════════════════════════════════════

def min_samples_for_significance(win_rate=0.55, avg_win=0.018,
                                 avg_loss=0.010, confidence=0.95):
    """
    估算判定"策略显著盈利"所需的最少交易笔数。

    原理：单笔盈亏是随机变量，n 笔的均值标准误 = σ/√n。
    要求 期望 - 1.645×标准误 > 0（单尾 95%）。

    这是回答"是运气还是实力"的定量工具。
    """
    p = win_rate
    exp = p * avg_win - (1 - p) * avg_loss
    if exp <= 0:
        return None          # 期望为负，永远无法验证
    # 方差 = E[X²] - E[X]²
    ex2 = p * (avg_win ** 2) + (1 - p) * (avg_loss ** 2)
    var = ex2 - exp ** 2
    sd = math.sqrt(var) if var > 0 else 0.0
    if sd <= 0:
        return None
    z = 1.645 if abs(confidence - 0.95) < 1e-9 else 2.326
    n = (z * sd / exp) ** 2
    return int(math.ceil(n))


def t_stat(n_trades, net_pnl, std_dev):
    """简化 t 统计量：判断净利是否显著非零"""
    if n_trades < 2 or std_dev <= 0:
        return 0.0
    return (net_pnl / n_trades) / (std_dev / math.sqrt(n_trades))


# ════════════════════════════════════════════════════════════
#  真实数据加载（在服务端运行，手机无法直接调用）
# ════════════════════════════════════════════════════════════

def _new_public_exchange(exchange_id="okx"):
    """
    新建一个【非沙盒】的交易所实例，专用于拉行情。

    ⚠️ 为什么必须独立建实例：
    ccxt 的 set_sandbox_mode(True) 会切换整个 urls，
    无法保证 fetch_ohlcv 仍返回真实市场数据。
    一旦模拟盘返回的是模拟行情，回测就是建立在假数据上 ——
    所有结论全部作废，而且极其隐蔽（数字看起来完全正常）。

    密钥在这里根本不需要：历史 K 线是公开数据，
    只有下单才要鉴权。不填密钥反而更安全。
    """
    import ccxt
    cls = getattr(ccxt, exchange_id, None)
    if cls is None:
        return None
    try:
        inst = cls({"enableRateLimit": True})
    except Exception:
        return None
    # 显式关闭沙盒，确保走真实公开接口
    try:
        if getattr(inst, "sandbox", False):
            inst.set_sandbox_mode(False)
    except Exception:
        pass
    return inst


async def fetch_bars(exchange, symbol, timeframe="15m", limit=1000,
                     public_exchange=None):
    """
    从交易所拉真实 K 线并转成 Bar 列表。

    ⚠️ 为什么这个函数必须存在：
    开发沙盒访问不了境外交易所（实测 OKX/Binance 全部 403），
    所以回测只能在【服务器上】用真实数据跑，
    再通过 Telegram 把结果发回手机。
    这是唯一能同时绕开"沙盒无网络"和"手机不能跑命令行"的方案。

    注意：这是公开行情接口，不需要 API key ——
    历史 K 线是公开数据，只有下单才需要鉴权。
    """
    import asyncio
    from core.bt_data import Bar

    loop = asyncio.get_event_loop()

    async def _try_fetch(e):
        if e is None:
            return None
        try:
            if getattr(e, "async_support", False):
                return await e.fetch_ohlcv(symbol, timeframe=timeframe,
                                           limit=limit)
            # 同步实例：丢进 executor，避免阻塞事件循环
            res = await loop.run_in_executor(
                None, lambda: e.fetch_ohlcv(symbol, timeframe, limit))
            # ⚠️ 兜底：有些异步实例没正确标记 async_support，
            # 在 executor 里调用会返回一个未被 await 的 coroutine
            #（表现为 'coroutine' object is not iterable，
            #  并伴随 RuntimeWarning: coroutine was never awaited）。
            # 这里补一次 await，兼容两种实现。
            if inspect.isawaitable(res):
                res = await res
            return res
        except Exception:
            return None

    # 按"真实性优先"排序逐个尝试，第一个成功的就是数据源。
    #
    # ⚠️ 曾在这里犯过错：只试 public_exchange，
    # 失败就直接返回空 —— 结果在公开实例不可用时
    # （比如测试环境无外网），连主 exchange 这个能用的
    # 数据源也被绕过了。必须保留完整回退链。
    candidates = [public_exchange, _new_public_exchange(), exchange]
    raw = None
    for cand in candidates:
        raw = await _try_fetch(cand)
        if raw:
            break

    # 释放临时实例
    for cand in candidates:
        if cand is not None and cand is not exchange \
                and cand is not public_exchange:
            try:
                if getattr(cand, "close", None):
                    await cand.close()
            except Exception:
                pass

    if not raw:
        return []
    bars = []
    for row in raw:
        # ccxt 格式: [timestamp, open, high, low, close, volume]
        bars.append(Bar(row[0], row[1], row[2], row[3], row[4],
                        row[5] if len(row) > 5 else 0.0))
    return bars


def split_regimes(bars, n_seg=3):
    """
    把一段行情切成若干段，用于分别检验不同走势下的表现。

    为什么必须分段：
    只看总收益会被"幸存者偏差"误导 ——
    如果整段行情恰好是上涨的，任何策略都显得不错。
    分段后才能看出：这段收益到底是来自策略，还是来自行情本身。
    """
    if not bars:
        return []
    seg_len = max(50, len(bars) // n_seg)
    segs = []
    for i in range(0, len(bars), seg_len):
        chunk = bars[i:i + seg_len]
        if len(chunk) < 50:
            continue
        first, last = chunk[0].close, chunk[-1].close
        change = (last - first) / first if first else 0
        if change > 0.03:
            label = "上涨"
        elif change < -0.03:
            label = "下跌"
        else:
            label = "震荡"
        segs.append({"label": label, "change": change, "bars": chunk})
    return segs


def run_regime_report(bars, cfg_factory):
    """
    分段回测，返回每段的摘要。
    cfg_factory: 返回 BTConfig 的可调用对象
    """
    out = []
    for seg in split_regimes(bars):
        r = Backtester(cfg_factory()).run(seg["bars"])
        out.append({
            "label": seg["label"],
            "change": seg["change"],
            "n_trades": r.n_trades,
            "net_pnl": r.net_pnl,
            "win_rate": r.win_rate,
            "max_dd": r.max_drawdown,
            "roi": r.roi,
        })
    return out
