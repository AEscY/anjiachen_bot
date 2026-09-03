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
                       equity_usdt: float) -> list:
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
        # ⚠️ 边界：原实现用【全额 equity】，不动用保留底线之外的判断；
        # 但回测引擎用的是 (cash - reserve_bottom)，两者不一致，
        # 导致回测显示"一单不挂"而实盘却挂了（或反之）。
        #
        # 统一为与回测一致的保守口径：先扣除保留底线再算每格。
        # reserve_bottom 的意义本就是"这钱不能动"，
        # 网格不应该把它算进可分配资金。
        budget = float(equity_usdt or 0.0) - float(
            getattr(self.cfg, "reserve_bottom", 0.0) or 0.0)
        budget = max(0.0, budget)
        per_usdt = budget * float(self.cfg.grid_capital_pct) / max(1, levels)
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

        st.lots[str(level)] = {
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
        lot = st.lots.pop(str(level), None)
        if lot is None:
            return None

        cost = float(lot["cost_usdt"])
        revenue = qty * price - (fee if fee_currency in ("", "USDT") else 0)
        pnl = revenue - cost
        st.realized_pnl += pnl
        st.fees += float(fee or 0)
        st.cycles += 1
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

    def should_stop_loss(self, symbol, price: float) -> bool:
        """击穿区间下限 且 整体浮亏超阈值"""
        st = self.states.get(symbol)
        if st is None or not st.lots:
            return False
        lower = getattr(st, "_lower_band", 0)
        if lower <= 0 or price >= lower:
            return False
        total_qty = sum(float(l["qty"]) for l in st.lots.values())
        total_cost = sum(float(l["cost_usdt"]) for l in st.lots.values())
        if total_qty <= 0:
            return False
        avg = total_cost / total_qty
        loss_pct = (avg - price) / avg
        return loss_pct >= float(self.cfg.grid_stop_loss_pct)
