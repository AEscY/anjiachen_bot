"""
交易所封装。

两个实例，这是必须的：
    ex   —— 交易用，可为模拟盘
    pub  —— 行情用，永远真实

为什么不能共用一个：ccxt 的 set_sandbox_mode(True) 会整体切换
urls，无法保证 fetch_ticker 仍返回真实市场数据。用模拟行情做
决策，等于在假数据上跑真钱。
"""
import logging

import ccxt

import config as C

logger = logging.getLogger(__name__)


def _make(sandbox: bool):
    cls = getattr(ccxt, C.EXCHANGE_ID)
    ex = cls({
        "apiKey": C.API_KEY,
        "secret": C.SECRET,
        "password": C.PASSPHRASE,
        "enableRateLimit": True,
        "options": {"defaultType": "spot"},
    })
    if sandbox:
        ex.set_sandbox_mode(True)
    return ex


class Exchange:
    def __init__(self):
        self.ex = _make(C.SANDBOX)
        self.pub = _make(False)
        self.markets = self.ex.load_markets()
        logger.info(f"交易所 {C.EXCHANGE_ID} 已连接"
                    f"（{'模拟盘' if C.SANDBOX else '实盘'}）")

    # ─────────── 精度 ───────────

    def round_qty(self, sym: str, qty: float) -> float:
        if qty <= 0:
            return 0.0
        return float(self.ex.amount_to_precision(sym, qty))

    def round_price(self, sym: str, price: float) -> float:
        return float(self.ex.price_to_precision(sym, price))

    def min_amount(self, sym: str) -> float:
        m = self.markets.get(sym) or {}
        lo = (m.get("limits") or {}).get("amount") or {}
        return float(lo.get("min") or 0.0)

    # ─────────── 行情（真实） ───────────

    def price(self, sym: str) -> float:
        """现价。失败返回 0（调用方必须检查，不能拿 0 去做决策）。"""
        try:
            t = self.pub.fetch_ticker(sym)
            return float(t.get("last") or 0.0)
        except Exception as e:
            logger.warning(f"取价失败 {sym}: {e}")
            return 0.0

    # ─────────── 账户 ───────────

    def balances(self) -> tuple[float, dict]:
        """返回 (可用 USDT, {币: 可用数量})"""
        try:
            b = self.ex.fetch_balance()
        except Exception as e:
            logger.warning(f"取余额失败: {e}")
            return 0.0, {}
        free_usdt = float((b.get("USDT") or {}).get("free") or 0.0)
        coins = {}
        for k, v in (b.get("free") or {}).items():
            if k == "USDT" or not isinstance(v, (int, float)):
                continue
            if float(v) > 0:
                coins[k] = float(v)
        return free_usdt, coins

    # ─────────── 订单 ───────────

    def open_orders(self, sym: str) -> list[dict]:
        try:
            return self.ex.fetch_open_orders(sym)
        except Exception as e:
            logger.warning(f"取挂单失败 {sym}: {e}")
            return []

    def fetch_order(self, oid: str, sym: str) -> dict | None:
        try:
            return self.ex.fetch_order(oid, sym)
        except Exception as e:
            logger.warning(f"查单失败 {sym} {oid}: {e}")
            return None

    def limit_buy(self, sym: str, qty: float, price: float) -> str | None:
        return self._place("buy", sym, qty, price)

    def limit_sell(self, sym: str, qty: float, price: float) -> str | None:
        return self._place("sell", sym, qty, price)

    def _place(self, side: str, sym: str, qty: float, price: float):
        if qty <= 0 or price <= 0:
            return None
        try:
            fn = self.ex.create_limit_buy_order if side == "buy" \
                else self.ex.create_limit_sell_order
            o = fn(sym, qty, price)
            return o.get("id")
        except Exception as e:
            logger.error(f"下单失败 {side} {sym} {qty}@{price}: {e}")
            return None

    def cancel(self, oid: str, sym: str) -> None:
        try:
            self.ex.cancel_order(oid, sym)
        except Exception as e:
            logger.warning(f"撤单失败 {sym} {oid}: {e}")

    def market_sell(self, sym: str, qty: float) -> str | None:
        """市价卖出，用于清仓。"""
        if qty <= 0:
            return None
        try:
            o = self.ex.create_market_sell_order(sym, qty)
            return o.get("id")
        except Exception as e:
            logger.error(f"市价卖出失败 {sym} {qty}: {e}")
            return None
