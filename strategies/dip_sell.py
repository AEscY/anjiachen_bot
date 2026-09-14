import asyncio
import logging
import time
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
        self.buy_px = self.base_px * merged["buyPct"]
        self.sell_px = self.base_px * merged["sellPct"]
        self.position = 0.0
        self.cost = 0.0
        self._last_action = None

        # 盈亏统计
        self.total_profit = 0.0
        self.total_fee = 0.0
        self.trade_count = 0

    async def start(self):
        self.running = True
        logger.info(f"{self.inst_id} 低吸高卖已启动，基准价 {self.base_px}")

    async def _sync_position_from_okx(self):
        """从 OKX 同步真实持仓"""
        try:
            base_ccy = self.inst_id.split("-")[0]
            bal_resp = await asyncio.to_thread(self.rest.get_balance, "USDT")
            if bal_resp.get("code") == "0" and bal_resp.get("data"):
                details = bal_resp["data"][0].get("details", [])
                for d in details:
                    if d.get("ccy") == base_ccy:
                        self.position = float(d.get("eq", 0))
                        self.cost = float(d.get("eqUsd", 0))
                        logger.info(f"{self.inst_id} 持仓已同步: {self.position}")
                        return
            self.position = 0.0
            self.cost = 0.0
        except Exception as e:
            logger.error(f"{self.inst_id} 同步持仓失败: {e}")

    async def _record_fee(self):
        """记录最近 30 秒内的成交手续费"""
        try:
            await asyncio.sleep(1.5)
            fills = await asyncio.to_thread(self.rest.get_fills, "SPOT", self.inst_id, 20)
            if fills.get("code") != "0":
                return
            now_ms = int(time.time() * 1000)
            for f in fills.get("data", []):
                try:
                    ts = int(f.get("ts", 0))
                    if now_ms - ts > 30000:
                        continue
                    fee = abs(float(f.get("fee", 0)))
                    self.total_fee += fee
                    self.trade_count += 1
                except (ValueError, TypeError):
                    continue
            logger.info(f"{self.inst_id} 手续费已更新: {self.total_fee:.4f}")
        except Exception as e:
            logger.error(f"{self.inst_id} 记录手续费失败: {e}")

    async def on_ticker(self, price: float, raw: dict):
        if not self.running:
            return

        # 低吸
        if self.position <= 0 and price <= self.buy_px:
            spend = self.params.get("maxSpend", 100)
            try:
                r = await asyncio.to_thread(self.rest.market_buy, self.inst_id, spend)
                if r.get("code") == "0":
                    self._last_action = ("buy", price)
                    logger.info(f"{self.inst_id} 买入请求已发送 @ {price}")
                    await self._record_fee()
                    await self._sync_position_from_okx()
            except Exception as e:
                logger.error(f"{self.inst_id} 买入失败: {e}")

        # 高卖
        elif self.position > 0 and price >= self.sell_px:
            sell_size = self.position
            sell_cost = self.cost
            try:
                r = await asyncio.to_thread(self.rest.market_sell, self.inst_id, sell_size)
                if r.get("code") == "0":
                    self._last_action = ("sell", price)
                    # 计算获利（简化算法：卖出金额 - 持仓成本）
                    profit = price * sell_size - sell_cost
                    self.total_profit += profit
                    logger.info(f"{self.inst_id} 卖出获利: {profit:.4f} USDT")
                    await self._record_fee()
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
            "total_profit": self.total_profit,
            "total_fee": self.total_fee,
            "trade_count": self.trade_count,
        })
        return s