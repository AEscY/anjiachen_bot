"""
网格算法。纯函数，不碰 IO —— 这样能直接单测，不需要 mock 交易所。

档位模型：
    中枢 center，间距 s，层数 n
    买档 i (0-based) 价格 = center × (1-s)^(i+1)
    该档成交后，卖单价 = 买价 × (1+s)

    例：center=100, s=2%, n=2
        买档0 = 98.00  → 卖 99.96
        买档1 = 96.04  → 卖 98.00
"""
import time


def buy_prices(center: float, spacing: float, levels: int) -> list[float]:
    """返回各档买入价，索引即档位号。"""
    return [center * (1 - spacing) ** (i + 1) for i in range(levels)]


def sell_price(buy_price: float, spacing: float) -> float:
    """买入成交瞬间锁定的卖单价。"""
    return buy_price * (1 + spacing)


def lot_pnl_pct(lot: dict, price: float) -> float:
    """该档位的浮动盈亏百分比。"""
    cost = lot.get("cost_usdt", 0.0)
    qty = lot.get("qty", 0.0)
    if cost <= 0 or qty <= 0:
        return 0.0
    avg = cost / qty
    return (price - avg) / avg


def effective_sell(
    lot: dict,
    price: float,
    spacing: float,
    follow: bool,
    follow_hours: float,
    follow_max_loss: float,
    now: float | None = None,
) -> float:
    """
    该档位此刻应有的卖单价。

    ══ 为什么要"跟随" ══

    卖单价在买入瞬间锁定，单边下跌时会变成死挂：

        center=100, s=2% → 买 98.04，卖单锁 100.00
        价格跌到 90 → 卖单仍挂 100.00，需涨 11% 才成交
        而买单在 88.24 继续吃货 → 仓位越滚越重，永不卖出

    实测（center=100, s=2%, 成本98.04, 现价90）：
        不跟随：卖 100.00（需涨 11.1%）  ❌ 死挂
        跟随：  卖  92.16（需涨  2.4%）  ✅ 可成交

    ══ 三条约束，缺一不可 ══

    ① 时间门槛（follow_hours，默认 24）
       持仓必须老化超过此值。防止刚买入就被微利洗出 ——
       那是旧版 v12 修过的 bug：0.7% 波动就被洗出，
       扣完手续费是净亏的。

    ② 亏损门槛（浮亏 > 间距 × 1.5）
       正常网格波动内不下移，只在明显套牢时才动。

    ③ 让利底线（follow_max_loss，默认 6%）
       卖单价不低于 成本 × (1 - 此值)。
       实测：现价跌到 20，卖单价仍稳在 92.16。

    follow=False 时始终返回锁定价（完全回到原始行为）。
    """
    locked = float(lot.get("sell_price", 0.0))
    if locked <= 0:
        return 0.0
    if not follow:
        return locked

    cost = float(lot.get("cost_usdt", 0.0))
    qty = float(lot.get("qty", 0.0))
    if cost <= 0 or qty <= 0:
        return locked
    avg = cost / qty

    # ① 时间门槛
    if follow_hours > 0:
        t0 = float(lot.get("buy_time", 0.0))
        if t0 <= 0:
            return locked
        age_h = ((now if now is not None else time.time()) - t0) / 3600.0
        if age_h < follow_hours:
            return locked

    # ② 亏损门槛
    loss = (avg - price) / avg if avg > 0 else 0.0
    if spacing > 0 and loss < spacing * 1.5:
        return locked

    # ③ 让利底线
    floor = avg * (1 - follow_max_loss)
    target = price * (1 + spacing * 0.5) if spacing > 0 else price
    new_px = max(floor, target)

    # 只能下移，绝不上移（不让利润目标变苛刻）
    return min(locked, new_px) if new_px < locked else locked


def below_stop(price: float, center: float, stop_loss: float) -> bool:
    """是否跌破区间止损线。中枢为 0 表示未建网。"""
    if center <= 0 or stop_loss <= 0:
        return False
    return price < center * (1 - stop_loss)
