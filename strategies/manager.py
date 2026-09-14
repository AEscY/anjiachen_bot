import asyncio
import logging
from okx_client.rest import OKXRest
from strategies.grid import GridStrategy
from strategies.dip_sell import DipSellStrategy
from config import GRID_QUOTE_SZ, DIP_MAX_SPEND

logger = logging.getLogger(__name__)


class StrategyManager:
    def __init__(self):
        self.rest = OKXRest()
        self.grids = {}
        self.dips = {}
        self._inst_ids = []

    def all_inst_ids(self):
        return list(self._inst_ids)

    async def add_inst(self, inst_id):
        inst_id = inst_id.upper().strip()
        if not inst_id or "-" not in inst_id:
            return False, "格式错误，示例: BTC-USDT"
        if inst_id in self.grids:
            return False, f"{inst_id} 已在监控中"

        try:
            resp = await asyncio.to_thread(self.rest.get_ticker, inst_id)
            if resp.get("code") != "0" or not resp.get("data"):
                return False, f"无法获取 {inst_id} 行情"
            price = float(resp["data"][0]["last"])
        except Exception as e:
            return False, f"获取行情失败: {e}"

        self.grids[inst_id] = GridStrategy(inst_id, {"quoteSz": GRID_QUOTE_SZ})
        self.dips[inst_id] = DipSellStrategy(inst_id, {"maxSpend": DIP_MAX_SPEND})
        self._inst_ids.append(inst_id)
        logger.info(f"已添加 {inst_id} @ {price}")
        return True, f"已添加 {inst_id}，当前价 {price}"

    async def remove_inst(self, inst_id):
        inst_id = inst_id.upper().strip()
        if inst_id not in self.grids:
            return False, f"{inst_id} 不在监控中"
        grid = self.grids.pop(inst_id)
        dip = self.dips.pop(inst_id)
        try:
            if grid.running:
                await grid.stop()
        except Exception as e:
            logger.error(f"停止 {inst_id} 网格失败: {e}")
        try:
            if dip.running:
                await dip.stop()
        except Exception as e:
            logger.error(f"停止 {inst_id} 低吸高卖失败: {e}")
        self._inst_ids.remove(inst_id)
        return True, f"已删除 {inst_id}"

    async def on_ticker(self, inst_id, price, raw):
        if inst_id in self.grids:
            await self.grids[inst_id].on_ticker(price, raw)
        if inst_id in self.dips:
            await self.dips[inst_id].on_ticker(price, raw)

    async def restore_all(self):
        for inst_id in self.all_inst_ids():
            try:
                resp = await asyncio.to_thread(self.rest.get_pending_grids, inst_id)
                if resp.get("code") == "0" and resp.get("data"):
                    latest = resp["data"][0]
                    grid = self.grids[inst_id]
                    grid.algo_id = latest["algoId"]
                    grid.params["minPx"] = float(latest.get("minPx", 0))
                    grid.params["maxPx"] = float(latest.get("maxPx", 0))
                    grid.params["gridNum"] = int(latest.get("gridNum", 0))
                    grid.running = True
                    logger.info(f"{inst_id} 网格已恢复: {latest['algoId']}")
            except Exception as e:
                logger.error(f"{inst_id} 网格恢复失败: {e}")