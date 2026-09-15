import asyncio
import logging
from okx_client.rest import OKXRest
from strategies.grid import GridStrategy
from strategies.dip_sell import DipSellStrategy
from core.risk_manager import RiskManager
from core.event_bus import bus
from core.events import RiskEvent, GridSubOrderEvent

logger = logging.getLogger(__name__)


class StrategyManager:
    def __init__(self, risk_manager=None):
        self.rest = OKXRest()
        self.grids = {}
        self.dips = {}
        self._inst_ids = []
        self.risk_manager = risk_manager or RiskManager()

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

        # 产品规格
        min_sz = 0.0
        try:
            spec = await asyncio.to_thread(self.rest.get_instruments, "SPOT", inst_id)
            if spec.get("code") == "0" and spec.get("data"):
                inst = spec["data"][0]
                min_sz = float(inst.get("minSz", 0))
        except Exception:
            pass

        min_notional = min_sz * price
        notes = []
        if min_notional > 0:
            notes.append(f"最小下单 {min_notional:.2f} USDT")

        self.grids[inst_id] = GridStrategy(inst_id)
        self.dips[inst_id] = DipSellStrategy(inst_id)
        self._inst_ids.append(inst_id)
        logger.info(f"已添加 {inst_id} @ {price}")

        msg = f"已添加 {inst_id}，当前价 {price}"
        if notes:
            msg += "\n" + "\n".join(notes)
        return True, msg

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
        """行情分发（受风控影响）"""
        if self.risk_manager.is_risk_triggered():
            return
        if inst_id in self.grids:
            await self.grids[inst_id].on_ticker(price, raw)
        if inst_id in self.dips:
            await self.dips[inst_id].on_ticker(price, raw)

    async def on_grid_sub_order(self, data: dict):
        """网格子订单回调，记录网格PnL"""
        try:
            algo_id = data.get("algoId", "")
            pnl = float(data.get("pnl", 0) or 0)
            fee = float(data.get("fee", 0) or 0)
            if pnl != 0:
                self.risk_manager.add_pnl(pnl)
            logger.info(f"网格子订单 algo={algo_id} pnl={pnl:.4f} fee={fee:.4f}")
        except Exception as e:
            logger.error(f"处理网格子订单失败: {e}")

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

    async def activate_grid_only(self, inst_id):
        inst_id = inst_id.upper().strip()
        grid = self.grids.get(inst_id)
        dip = self.dips.get(inst_id)
        if not grid:
            return False, "网格未初始化"
        if dip and dip.running:
            await dip.stop()
        if not grid.running:
            try:
                await grid.start()
            except Exception as e:
                return False, f"网格启动失败: {e}"
        return True, "网格已启动，低吸高卖已自动暂停"

    async def activate_dip_only(self, inst_id):
        inst_id = inst_id.upper().strip()
        grid = self.grids.get(inst_id)
        dip = self.dips.get(inst_id)
        if not dip:
            return False, "低吸高卖未初始化"
        if grid and grid.running:
            await grid.stop()
        if not dip.running:
            try:
                await dip.start()
            except Exception as e:
                return False, f"低吸高卖启动失败: {e}"
        return True, "低吸高卖已启动，网格已自动停止"

    async def stop_all_strategies(self, inst_id):
        inst_id = inst_id.upper().strip()
        grid = self.grids.get(inst_id)
        dip = self.dips.get(inst_id)
        stopped = []
        if grid and grid.running:
            await grid.stop()
            stopped.append("网格")
        if dip and dip.running:
            await dip.stop()
            stopped.append("低吸高卖")
        return True, f"已停止: {', '.join(stopped)}" if stopped else "两个策略均未运行"