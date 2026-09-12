# strategies/dip_sell.py
from strategies.base import BaseStrategy
from okx_client.rest import OKXRest
from config import DIP_DEFAULT, MAX_POSITION_USDT

class DipSellStrategy(BaseStrategy):
    def __init__(self, inst_id: str, params: dict | None = None):
        super().__init__(inst_id, {**DIP_DEFAULT, **(params or {})})
        self.rest = OKXRest()
        self.base_px = self.params["basePx"]
        self.buy_px  = self.base_px * self.params["buyPct"]
        self.sell_px = self.base_px * self.params["sellPct"]
        self.position = 0.0           # 持仓数量（base 币种）
        self.cost = 0.0               # 持仓成本（USDT）
        self._last_action = None

    async def start(self):
        self.running = True

    async def on_ticker(self, price: float, raw: dict):
        if not self.running:
            return

        # 低吸：无持仓且价格跌破买入线
        if self.position <= 0 and price <= self.buy_px:
            spend = min(MAX_POSITION_USDT, self.params.get("maxSpend", MAX_POSITION_USDT))
            r = self.rest.market_buy(self.inst_id, spend)
            if r.get("code") == "0":
                # 实际成交数量需从订单推送或 REST 查询确认，此处为占位
                self.position = spend / price
                self.cost = spend
                self._last_action = ("buy", price)

        # 高卖：有持仓且价格突破卖出线
        elif self.position > 0 and price >= self.sell_px:
            r = self.rest.market_sell(self.inst_id, self.position)
            if r.get("code") == "0":
                self.position = 0.0
                self.cost = 0.0
                self._last_action = ("sell", price)

    async def stop(self):
        self.running = False

    def snapshot(self):
        s = super().snapshot()
        s.update({
            "base_px": self.base_px, "buy_px": self.buy_px,
            "sell_px": self.sell_px, "position": self.position,
            "last_action": self._last_action,
        })
        return s