"""
grid.py - 网格引擎（核心）

设计要点
────────
1. 声明式：网格只计算「此刻应该挂哪些单」，不直接下单。
   由 execution 层负责把目标状态同步到交易所。
   好处：重启后重新计算目标状态 → 与交易所未成交单对比 → 自动补齐/撤销，
   天然实现「启动对账」，不需要单独写恢复逻辑。

2. 每档独立配对：第 i 档买入成交后，立即在 买入价×(1+该档间距) 挂卖单。
   盈利来自每一格价差，而非趋势波段。

3. 中枢策略（回测结论）：间距随 ATR 动态伸缩，但中枢锚定。
   中枢跟随会在单边行情中追着价格跑，实测回撤 12.0% vs 7.5%、止损率 57% vs 36%。

4. 持仓档的卖单价在买入时锁定，不随后续间距变化而改变 —— 保证每格利润确定。
"""
import time
import logging
from dataclasses import dataclass, field, asdict
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class GridOrder:
    """一条期望挂出的订单（描述目标状态，非交易所返回值）"""
    level: int
    side: str            # 'buy' / 'sell'
    price: float
    qty: float           # 卖单用币数量；买单也用币数量
    usdt: float = 0.0    # 预计占用 USDT（买单）
    client_id: str = ""  # 幂等键


@dataclass
class GridLot:
    """某一档的持仓"""
    level: int
    qty: float
    cost_usdt: float
    buy_price: float
    sell_price: float      # 买入时锁定，不随间距变化
    buy_time: float = 0.0
    order_id: str = ""


@dataclass
class GridState:
    """单个币种的网格状态（可 JSON 序列化后持久化）"""
    symbol: str
    anchor: float = 0.0          # 锚点（anchored 模式下不移动）
    center: float = 0.0          # 当前中枢
    spacing: float = 0.0         # 当前间距（百分比）
    lots: dict = field(default_factory=dict)   # {level_str: GridLot dict}
    realized_pnl: float = 0.0
    fees: float = 0.0
    cycles: int = 0              # 完成的网格循环数
    created_at: float = 0.0
    last_rebalance: float = 0.0
    pending_client_ids: dict = field(default_factory=dict)  # {client_id: level}

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        if not d:
            return None
        s = cls(symbol=d.get("symbol", ""))
        for k in ("anchor", "center", "spacing", "realized_pnl", "fees",
                  "cycles", "created_at", "last_rebalance"):
            setattr(s, k, float(d.get(k, 0) or 0))
        s.lots = d.get("lots", {}) or {}
        s.pending_client_ids = d.get("pending_client_ids", {}) or {}
        return s


class GridEngine:
    """
    网格引擎：给定最新价与波动率，输出应挂订单列表。

    用法：
        st = engine.ensure_state(sym, price)        # 初始化/加载状态
        orders = engine.desired_orders(sym, price, atr_pct, equity_usdt)
        # → 交给 execution.reconcile(orders) 同步到交易所
        engine.on_buy_filled(sym, level, qty, price)   # 成交回调
        engine.on_sell_filled(sym, level, price)       # 成交回调
    """

    def __init__(self, cfg, exchange):
        """
        cfg: 提供参数访问的对象（QuantBot 自身即可）
        exchange: ExchangeManager，用于精度换算
        """
        self.cfg = cfg
        self.exchange = exchange
        self.states: dict = {}

    # ─────────── 状态管理 ───────────

    def get_state(self, symbol) -> Optional[GridState]:
        return self.states.get(symbol)

    def ensure_state(self, symbol, price: float) -> GridState:
        """首次调用时以当前价为锚点建立网格"""
        st = self.states.get(symbol)
        if st is None:
            st = GridState(symbol=symbol)
            st.anchor = price
            st.center = price
            # 初始化间距：on_buy_filled 依赖 st.spacing 锁定卖单价，
            # 若此处为 0，首次成交前未调用过 desired_orders 时会静默回退到基础间距
            st.spacing = self.calc_spacing(0.0)
            st.created_at = time.time()
            st.last_rebalance = time.time()
            self.states[symbol] = st
            logger.info(f"🕸️  建立网格 {symbol}: 锚点 {price:.4f} "
                        f"间距 {st.spacing*100:.2f}%")
        return st

    def reset(self, symbol, price: float):
        """重置网格（清仓后重建）"""
        self.states.pop(symbol, None)
        return self.ensure_state(symbol, price)

    def remove(self, symbol):
        self.states.pop(symbol, None)

    # ─────────── 间距计算 ───────────

    def calc_spacing(self, atr_pct: float) -> float:
        """
        当前间距（百分比小数）。
        fixed   → 用 grid_spacing_pct
        atr     → ATR% × 倍数，并夹在 [min, max] 之间
        """
        base = float(self.cfg.grid_spacing_pct)
        if str(self.cfg.grid_spacing_mode).lower() == "fixed":
            return base

        raw = float(atr_pct or 0) * float(self.cfg.grid_atr_mult)
        lo = float(self.cfg.grid_spacing_min)
        hi = float(self.cfg.grid_spacing_max)
        # ATR 缺失时回退到基础间距
        if raw <= 0:
            return max(lo, min(hi, base))
        return max(lo, min(hi, raw))

    # ─────────── 中枢管理 ───────────

    def update_center(self, st: GridState, price: float, spacing: float):
        """
        按模式更新中枢。
        anchored  → 中枢可漂移，但相对锚点偏移不超过 grid_max_drift_pct
        following → 中枢直接跟随（回测显示效果最差，保留以供对比）
        """
        mode = str(self.cfg.grid_anchor_mode).lower()
        if mode == "following":
            st.center = price
            return False

        # anchored：限幅漂移 —— 允许适应温和位移，但死守最大偏移
        max_drift = float(self.cfg.grid_max_drift_pct)
        if st.anchor > 0:
            drift = (price - st.anchor) / st.anchor
            drift = max(-max_drift, min(max_drift, drift))
            st.center = st.anchor * (1 + drift)
        else:
            st.center = price
        return True

    def need_rebalance(self, st: GridState, price: float, spacing: float) -> bool:
        """中枢漂移是否超过阈值（以「格」为单位衡量）"""
        if st.center <= 0 or spacing <= 0:
            return True
        if time.time() - st.last_rebalance < float(self.cfg.grid_rebalance_interval):
            return False
        drift_levels = abs(price - st.center) / (st.center * spacing)
        return drift_levels >= float(self.cfg.grid_rebalance_drift)

    # ─────────── 档位计算 ───────────

    def level_prices(self, st: GridState, spacing: float, levels: int):
        """返回买入档价格列表（由高到低，长度 = levels）"""
        c = st.center
        return [c * (1 - spacing) ** (i + 1) for i in range(levels)]

    # ─────────── 目标订单（核心）───────────

    def desired_orders(self, symbol, price: float, atr_pct: float,
                       equity_usdt: float, budget_is_net: bool = False) -> list:
        """
        计算此刻应该挂在交易所的订单。

        每个档位二选一：
          空档   → 在买档价挂 买单
          持仓档 → 在买入时锁定的卖单价 挂 卖单（全部持仓）
        """
        st = self.ensure_state(symbol, price)
        levels = int(self.cfg.grid_levels)
        spacing = self.calc_spacing(atr_pct)
        st.spacing = spacing

        # 中枢漂移超限 → 重挂
        if self.need_rebalance(st, price, spacing):
            self.update_center(st, price, spacing)
            st.last_rebalance = time.time()
            logger.info(f"🔄 {symbol} 网格重挂: 中枢 {st.center:.4f} 间距 {spacing*100:.2f}%")

        buys = self.level_prices(st, spacing, levels)
        lower_band = buys[-1] * (1 - float(self.cfg.grid_lower_buffer_pct))
        upper_band = buys[0] * (1 + float(self.cfg.grid_upper_buffer_pct))

        # 每档金额
        #
        # budget_is_net=True 表示调用方传入的 equity_usdt【已是该币种的
        # 可支配预算】（已扣除保留底线、已按币种数平分），此时不得再扣一次。
        #
        # 为什么需要这个开关（真实事故）：
        #   _grid_monitor 传的是 share = equity / 币种数，
        #   而这里又扣了一次 reserve_bottom —— 每个币种各扣一遍，
        #   3 个币种就等于扣了 3 倍底线。更严重的是 equity 用的是
        #   【总权益】（含持仓市值），不是【可用 USDT 现金】。
        #
        # 实测用户账户：现金 4,998 U，持仓市值 85,531 U，
        #   总权益 90,529 U → 每格算出 12,070 U
        #   3 币 × 2 层需要 72,421 U，而实际只有 4,998 U
        #   → 超额 14.5 倍，SOL 抢先挂出巨额单，其余币种余额不足静默失败。
        if budget_is_net:
            budget = max(0.0, float(equity_usdt or 0.0))
        else:
            budget = float(equity_usdt or 0.0) - float(
                getattr(self.cfg, "reserve_bottom", 0.0) or 0.0)
            budget = max(0.0, budget)
        per_usdt = budget * float(self.cfg.grid_capital_pct) / max(1, levels)

        # 单格金额上限：防止配置错误（或权益口径异常）挂出巨额单。
        # 0 = 不限制。
        cap_order = float(getattr(self.cfg, "grid_max_order_usdt", 0) or 0)
        if cap_order > 0 and per_usdt > cap_order:
            per_usdt = cap_order

        min_order = float(self.cfg.grid_min_order_usdt)

        # 静默失败可视化：原来每格不达标时直接 continue，
        # 用户看到"网格是空的"却查不到原因。
        if per_usdt > 0 and per_usdt < min_order:
            logger.warning(
                f"⚠️ {symbol} 网格每格 {per_usdt:.4f}U < 下限 {min_order:.4f}U，"
                f"该币种本轮不挂单。"
                f"（预算 {budget:.2f}U / {levels}层 × 仓位{float(self.cfg.grid_capital_pct)*100:.0f}%）"
                f" 可调 /setminorder 或 /setlevels 或减少币种数")

        orders = []
        for i in range(levels):
            lot = st.lots.get(str(i))

            if lot is None:
                # 空档：价格未触及买档 → 不挂单（等价格跌下来再挂）
                # 真网格会提前挂限价单等待成交
                bp = buys[i]
                if price <= bp:
                    continue          # 已跌破，由成交回调处理，避免重复挂
                if price > upper_band:
                    continue          # 高于区间上限，不追高
                if per_usdt < min_order:
                    continue
                qty = per_usdt / bp
                orders.append(GridOrder(
                    level=i, side="buy", price=bp, qty=qty, usdt=per_usdt,
                    client_id=f"g{symbol.replace('/', '')[:8]}B{i}",
                ))
            else:
                # 持仓档：挂卖单（用买入时锁定的卖单价）
                orders.append(GridOrder(
                    level=i, side="sell", price=float(lot["sell_price"]),
                    qty=float(lot["qty"]),
                    client_id=f"g{symbol.replace('/', '')[:8]}S{i}",
                ))

        # 击穿下限 → 返回止损信号（由风控层执行市价清仓）
        st._lower_band = lower_band
        st._upper_band = upper_band
        return orders

    # ─────────── 成交回调 ───────────

    def on_buy_filled(self, symbol, level: int, qty: float, price: float,
                      fee: float = 0.0, fee_currency: str = "", order_id: str = ""):
        """买单成交：建立持仓，并按买入时的间距锁定卖单价"""
        st = self.states.get(symbol)
        if st is None:
            return
        spacing = st.spacing or float(self.cfg.grid_spacing_pct)
        base = symbol.split("/")[0]
        # 以币抵扣手续费时先扣除再入账；必须夹到非负，
        # 否则手续费异常（或 mock 数据失真）会写入负持仓，
        # 后续挂卖单时 qty<=0 被静默跳过，档位永远卡死
        net_qty = max(0.0, qty - fee) if fee_currency == base else qty
        cost = qty * price + (fee if fee_currency in ("", "USDT") else 0)

        if net_qty <= 0:
            logger.error(f"❌ {symbol} 第{level}档买入后净数量为0"
                         f"(成交{qty:.6f} 手续费{fee:.6f}{fee_currency})，放弃建仓")
            return None

        key = str(level)
        old_lot = st.lots.get(key)

        # ⚠️ 同档位已有持仓时必须【合并】，绝不能覆盖。
        #
        # 原实现直接 st.lots[key] = {...} 覆盖，后果（实测）：
        #     第1次买入 10 SOL @ 98  → 成本 980 U
        #     中枢漂移后第2次买入 10 SOL @ 88 → 成本 880 U
        #     账本只剩 10 SOL / 880 U
        #     → 第1次的 10 SOL（980 U）【凭空消失】
        #     → 账本与交易所差 980 U → 对账阻塞
        #
        # 触发场景：买单成交 → 卖单未成交 → 中枢漂移重挂
        #          → 同档位新买单价更低 → 再次成交
        if old_lot is not None:
            oq = float(old_lot.get("qty") or 0)
            oc = float(old_lot.get("cost_usdt") or 0)
            nq = oq + float(net_qty)
            nc = oc + float(cost)
            if nq <= 0:
                logger.error(f"❌ {symbol} 第{level}档合并后数量异常，放弃")
                return None
            new_avg = nc / nq
            old_lot["qty"] = float(nq)
            old_lot["cost_usdt"] = float(nc)
            old_lot["buy_price"] = float(new_avg)
            # 卖单价按【加权成本】重算，保证整批仍有确定利润空间；
            # 若沿用旧的更高价，价格长期低于它时卖单永不成交。
            old_lot["sell_price"] = float(new_avg) * (1 + spacing)
            old_lot["buy_time"] = time.time()
            old_lot["order_id"] = order_id
            # 强制重新挂单：原卖单价已变，需撤旧单重挂
            st.pending_client_ids.pop(key, None)
            st.fees += float(fee or 0)
            logger.warning(
                f"🔀 {symbol} 第{level}档【合并】买入 {net_qty:.6f} @ {price:.4f}"
                f"（原 {oq:.6f} @ 均{oc/max(oq,1e-12):.4f}）"
                f"→ 现 {nq:.6f} 均价 {new_avg:.4f} → 挂卖 {old_lot['sell_price']:.4f}")
            return 0.0

        st.lots[key] = {
            "level": level, "qty": float(net_qty), "cost_usdt": float(cost),
            "buy_price": float(price),
            "sell_price": float(price) * (1 + spacing),   # 锁定
            "buy_time": time.time(), "order_id": order_id,
        }
        st.fees += float(fee or 0)
        logger.info(f"🕸️  {symbol} 第{level}档买入 {qty:.6f} @ {price:.4f} "
                    f"→ 挂卖 {price*(1+spacing):.4f}")

    def on_sell_filled(self, symbol, level: int, qty: float, price: float,
                       fee: float = 0.0, fee_currency: str = ""):
        """卖单成交：了结该档，记录已实现盈亏"""
        st = self.states.get(symbol)
        if st is None:
            return None
        key = str(level)
        lot = st.lots.get(key)
        if lot is None:
            return None

        lot_qty = float(lot.get("qty") or 0)
        lot_cost = float(lot.get("cost_usdt") or 0)
        if lot_qty <= 0:
            st.lots.pop(key, None)
            return None

        sell_qty = float(qty or 0)
        if sell_qty <= 0:
            return None

        # ⚠️ 部分成交必须按比例分摊成本。
        #
        # 原实现 cost = 整档总成本，而 revenue 只算卖出的那部分：
        #     买入 10 SOL @ 100（成本 1000 U），卖出 3 SOL @ 102
        #     原算法：306 − 1000 = −694 U   ❌ 明明赚了却记巨亏
        #     正确  ：306 − 300  = +6 U
        #
        # 更严重：原实现无条件 lots.pop()，剩余 7 SOL 的持仓
        # 【凭空消失】→ 账本与交易所不一致 → 对账阻塞。
        #
        # 限价单部分成交在实盘很常见（流动性不足/大单），必然踩中。
        if sell_qty >= lot_qty - 1e-9:
            cost = lot_cost
            st.lots.pop(key, None)
            remaining = 0.0
        else:
            ratio = sell_qty / lot_qty
            cost = lot_cost * ratio
            lot["qty"] = lot_qty - sell_qty
            lot["cost_usdt"] = lot_cost - cost
            remaining = lot["qty"]

        revenue = sell_qty * price - (fee if fee_currency in ("", "USDT") else 0)
        pnl = revenue - cost
        st.realized_pnl += pnl
        st.fees += float(fee or 0)
        st.cycles += 1
        if remaining > 0:
            logger.info(
                f"🕸️  {symbol} 第{level}档【部分】卖出 {sell_qty:.6f}/{lot_qty:.6f} "
                f"@ {price:.4f} 净利 {pnl:+.4f}U，剩余 {remaining:.6f}"
                f"（累计 {st.realized_pnl:+.4f}U）")
        else:
            logger.info(f"🕸️  {symbol} 第{level}档卖出 @ {price:.4f} 净利 {pnl:+.4f}U "
                        f"(累计 {st.realized_pnl:+.4f}U)")
        return pnl

    # ─────────── 查询 ───────────

    def stats(self, symbol, price: float) -> dict:
        st = self.states.get(symbol)
        if st is None:
            return {}
        unrealized = 0.0
        total_qty = 0.0
        total_cost = 0.0
        for lot in st.lots.values():
            q = float(lot["qty"])
            total_qty += q
            total_cost += float(lot["cost_usdt"])
            unrealized += q * price - float(lot["cost_usdt"])
        return {
            "symbol": symbol,
            "anchor": st.anchor,
            "center": st.center,
            "spacing_pct": st.spacing * 100,
            "lots": len(st.lots),
            "total_qty": total_qty,
            "total_cost": total_cost,
            "market_value": total_qty * price,
            "unrealized": unrealized,
            "realized": st.realized_pnl,
            "fees": st.fees,
            "cycles": st.cycles,
            "lower_band": getattr(st, "_lower_band", 0),
            "upper_band": getattr(st, "_upper_band", 0),
        }

    def stop_loss_band(self, st: GridState, spacing: float = 0.0):
        """
        基于【锚点 anchor】的固定止损下界。

        为什么必须用 anchor 而不是 center：

        center 会随价格漂移（need_rebalance → update_center），
        而 _lower_band 正是由 center 算出的 —— 于是价格每跌一段，
        中枢跟着下移，下界也跟着下移，价格【永远跌不破】下界。

        实测（中枢100/间距2%/2层/止损5%）：
            现价  96   center  96   下界 90.35  浮亏 1.09%   不止损
            现价  92   center  92   下界 86.59  浮亏 5.21%   不止损
            现价  85   center  85   下界 80.00  浮亏 12.43%  不止损
        → 浮亏 12.43% 远超 5% 阈值，却因"没跌破下界"永不触发，
          网格在低位继续买入，一路接刀到底。

        anchor 在建网时确定后【不再变化】，用它算出的下界是固定的，
        价格单边下跌时必然跌破 → 止损正常生效。
        """
        sp = spacing or float(st.spacing or 0) or self.calc_spacing(0.0)
        if sp <= 0:
            return 0.0
        base = float(st.anchor or 0) or float(st.center or 0)
        if base <= 0:
            return 0.0
        levels = max(1, int(getattr(self.cfg, "grid_levels", 2) or 2))
        lowest_buy = base * (1 - sp) ** levels
        return lowest_buy * (1 - float(self.cfg.grid_lower_buffer_pct))

    def should_stop_loss(self, symbol, price: float) -> bool:
        """
        击穿区间下限 且 整体浮亏超阈值 → 止损。

        两处修复（都是致命的）：

        ① 原依赖 st._lower_band —— 它是 desired_orders 的【副产品】：
           · 不在 dataclass 字段里，to_dict/from_dict 不持久化
           · 风控拦截 / 行情陈旧 / 对账阻塞时 desired_orders 不执行
             → 值为 0 → `if lower <= 0: return False` 直接放行
           → 重启后第一轮、或任何跳过挂单的轮次，止损彻底失效。

        ② 即使有值，它也随 center 漂移（见 stop_loss_band 的说明），
           导致单边下跌时永不止损。

        现在改为基于【anchor 的固定下界】独立计算，不再依赖任何副产品。
        """
        st = self.states.get(symbol)
        if st is None or not st.lots:
            return False

        total_qty = sum(float(l["qty"]) for l in st.lots.values())
        total_cost = sum(float(l["cost_usdt"]) for l in st.lots.values())
        if total_qty <= 0 or total_cost <= 0:
            return False

        avg = total_cost / total_qty
        loss_pct = (avg - price) / avg
        threshold = float(self.cfg.grid_stop_loss_pct)

        # 绝对兜底：浮亏超过硬止损线（0=关闭），无论如何都止损。
        # 防止 anchor 被异常重置、或参数配置失当时保护完全失效。
        hard = float(getattr(self.cfg, "grid_hard_stop_loss_pct", 0) or 0)
        if hard > 0 and loss_pct >= hard:
            logger.warning(
                f"🛑 {symbol} 触发硬止损：浮亏 {loss_pct*100:.2f}% "
                f"≥ {hard*100:.2f}%（无视区间下界）")
            return True

        # 区间止损：基于 anchor 的固定下界
        lower = self.stop_loss_band(st)
        if lower <= 0 or price >= lower:
            return False
        return loss_pct >= threshold

    def loss_info(self, symbol, price: float) -> dict:
        """供 /grid 显示的止损诊断信息"""
        st = self.states.get(symbol)
        if st is None or not st.lots:
            return {}
        total_qty = sum(float(l["qty"]) for l in st.lots.values())
        total_cost = sum(float(l["cost_usdt"]) for l in st.lots.values())
        if total_qty <= 0:
            return {}
        avg = total_cost / total_qty
        return {
            "avg_cost": avg,
            "loss_pct": (avg - price) / avg,
            "band": self.stop_loss_band(st),
            "anchor": st.anchor,
            "center": st.center,
            "threshold": float(self.cfg.grid_stop_loss_pct),
            "hard": float(getattr(self.cfg, "grid_hard_stop_loss_pct", 0) or 0),
        }
