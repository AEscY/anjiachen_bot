# strategies/dip_sell.py
import logging
from strategies.base import BaseStrategy
from okx_client.rest import OKXRest

logger = logging.getLogger(__name__)


class DipSellStrategy(BaseStrategy):
    def __init__(self, inst_id: str, params: dict | None = None):
        default = {
            "basePx": 0,
            "buyPct": 0.98,
            "sellPct": 1.03,
            "maxSpend": 100,
        }
        merged = {**default, **(params or {})}
        super().__init__(inst_id, merged)

        self.rest = OKXRest()
        self.base_px = merged["basePx"]
        self.buy_px  = self.base_px * merged["buyPct"]
        self.sell_px = self.base_px * merged["sellPct"]
        self.position = 0.0
        self.cost = 0.0
        self._last_action = None

    async def start(self):
        self.running = True
        logger.info(f"{self.inst_id} 低吸高卖已启动，基准价 {self.base_px}")

    async def on_ticker(self, price: float, raw: dict):
        if not self.running:
            return

        # 低吸
        if self.position <= 0 and price <= self.buy_px:
            spend = self.params.get("maxSpend", 100)
            try:
                r = self.rest.market_buy(self.inst_id, spend)
                if r.get("code") == "0":
                    self.position = spend / price
                    self.cost = spend
                    self._last_action = ("buy", price)
                    logger.info(f"{self.inst_id} 买入 @ {price}")
            except Exception as e:
                logger.error(f"{self.inst_id} 买入失败: {e}")

        # 高卖
        elif self.position > 0 and price >= self.sell_px:
            try:
                r = self.rest.market_sell(self.inst_id, self.position)
                if r.get("code") == "0":
                    self.position = 0.0
                    self.cost = 0.0
                    self._last_action = ("sell", price)
                    logger.info(f"{self.inst_id} 卖出 @ {price}")
            except Exception as e:
                logger.error(f"{self.inst_id} 卖出失败: {e}")

    async def stop(self):
        self.running = False

    def snapshot(self):
        s = super().snapshot()
        s.update({
            "base_px": self.base_px,
            "buy_px": self.buy_px,
            "sell_px": self.sell_px,
            "position": self.position,
            "last_action": self._last_action,
        })
        return s