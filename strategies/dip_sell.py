# strategies/dip_sell.py
import asyncio
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

    async def _sync_position_from_okx(self):
        """从 OKX 真实账户同步当前持仓，避免下单后假设成交导致数据不准"""
        try:
            base_ccy = self.inst_id.split("-")[0]
            # 使用 asyncio.to_thread 避免同步请求阻塞事件循环
            bal_resp = await asyncio.to_thread(self.rest.get_balance, "USDT")
            if bal_resp.get("code") == "0" and bal_resp.get("data"):
                details = bal_resp["data"][0].get("details", [])
                for d in details:
                    if d.get("ccy") == base_ccy:
                        self.position = float(d.get("eq", 0))
                        self.cost = float(d.get("eqUsd", 0))
                        logger.info(f"{self.inst_id} 真实持仓已同步: {self.position}")
                        return
            # 如果没找到对应币种，说明持仓为 0
            self.position = 0.0
            self.cost = 0.0
        except Exception as e:
            logger.error(f"{self.inst_id} 同步持仓失败: {e}")

    async def on_ticker(self, price: float, raw: dict):
        if not self.running:
            return

        # 低吸：无持仓且价格跌破买入线
        if self.position <= 0 and price <= self.buy_px:
            spend = self.params.get("maxSpend", 100)
            try:
                r = await asyncio.to_thread(self.rest.market_buy, self.inst_id, spend)
                if r.get("code") == "0":
                    self._last_action = ("buy", price)
                    logger.info(f"{self.inst_id} 买入请求已发送 @ {price}")
                    # 下单后，同步真实持仓
                    await asyncio.sleep(1)  # 给成交和账变一点时间
                    await self._sync_position_from_okx()
            except Exception as e:
                logger.error(f"{self.inst_id} 买入失败: {e}")

        # 高卖：有持仓且价格突破卖出线
        elif self.position > 0 and price >= self.sell_px:
            try:
                r = await asyncio.to_thread(self.rest.market_sell, self.inst_id, self.position)
                if r.get("code") == "0":
                    self._last_action = ("sell", price)
                    logger.info(f"{self.inst_id} 卖出请求已发送 @ {price}")
                    # 下单后，同步真实持仓
                    await asyncio.sleep(1)
                    await self._sync_position_from_okx()
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