import asyncio
import logging
from okx_client.rest import OKXRest
from strategies.dip_sell import DipSellStrategy
from core.risk_manager import RiskManager

logger = logging.getLogger(__name__)


class StrategyManager:
    """只管理低吸高卖策略的管理器"""

    def __init__(self, risk_manager=None):
        self.rest = OKXRest()
        self.grids = {}   # 保留空字典，兼容 dashboard 引用
        self.dips = {}
        self._inst_ids = []
        self.risk_manager = risk_manager or RiskManager()

    def all_inst_ids(self):
        return list(self._inst_ids)

    async def add_inst(self, inst_id):
        inst_id = inst_id.upper().strip()
        if not inst_id or "-" not in inst_id:
            return False, "格式错误，示例: BTC-USDT"
        if inst_id in self.dips:
            return False, f"{inst_id} 已在监控中"

        try:
            resp = await asyncio.to_thread(self.rest.get_ticker, inst_id)
            if resp.get("code") != "0" or not resp.get("data"):
                return False, f"无法获取 {inst_id} 行情"
            price = float(resp["data"][0]["last"])
        except Exception as e:
            return False, f"获取行情失败: {e}"

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

        self.dips[inst_id] = DipSellStrategy(inst_id)
        self._inst_ids.append(inst_id)
        logger.info(f"已添加 {inst_id} @ {price}")

        msg = f"已添加 {inst_id}，当前价 {price}"
        if notes:
            msg += "\n" + "\n".join(notes)
        return True, msg

    async def remove_inst(self, inst_id):
        inst_id = inst_id.upper().strip()
        if inst_id not in self.dips:
            return False, f"{inst_id} 不在监控中"

        dip = self.dips.pop(inst_id)
        try:
            if dip.running:
                await dip.stop()
        except Exception as e:
            logger.error(f"停止 {inst_id} 低吸高卖失败: {e}")

        self._inst_ids.remove(inst_id)
        return True, f"已删除 {inst_id}"

    async def on_ticker(self, inst_id, price, raw):
        if self.risk_manager.is_risk_triggered():
            return
        if inst_id in self.dips:
            await self.dips[inst_id].on_ticker(price, raw)

    async def restore_all(self):
        """启动时同步所有币种的持仓状态"""
        for inst_id in self.all_inst_ids():
            dip = self.dips.get(inst_id)
            if not dip:
                continue
            try:
                await dip._sync_position_from_okx()
                logger.info(f"{inst_id} 持仓已同步: {dip.position}")
            except Exception as e:
                logger.error(f"{inst_id} 持仓同步失败: {e}")

    async def activate_dip_only(self, inst_id):
        inst_id = inst_id.upper().strip()
        dip = self.dips.get(inst_id)
        if not dip:
            return False, "低吸高卖未初始化"
        if not dip.running:
            try:
                await dip.start()
            except Exception as e:
                return False, f"启动失败: {e}"
        return True, "低吸高卖已启动"

    async def stop_all_strategies(self, inst_id):
        inst_id = inst_id.upper().strip()
        dip = self.dips.get(inst_id)
        if not dip:
            return False, "未初始化"
        if dip.running:
            await dip.stop()
            return True, "已停止低吸高卖"
        return True, "低吸高卖未运行"