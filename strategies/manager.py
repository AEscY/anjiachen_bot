import asyncio
import logging
from okx_client.rest import OKXRest
from strategies.dip_sell import DipSellStrategy
from core.risk_manager import RiskManager
from core.state_store import StateStore

logger = logging.getLogger(__name__)


class StrategyManager:
    def __init__(self, risk_manager=None, state_store=None):
        self.rest = OKXRest()
        self.dips = {}
        self._inst_ids = []
        self.risk_manager = risk_manager or RiskManager()
        self.state_store = state_store or StateStore()
        self._save_task = None

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

        # 从 Gist 恢复状态
        saved = await self.state_store.load_strategy(inst_id)
        if saved:
            dip = self.dips[inst_id]
            dip.position = saved.get("position", 0.0)
            dip.avg_buy_price = saved.get("avg_buy_price", 0.0)
            dip.peak_price = saved.get("peak_price", 0.0)
            dip.total_profit = saved.get("total_profit", 0.0)
            dip.total_fee = saved.get("total_fee", 0.0)
            dip.trade_count = saved.get("trade_count", 0)
            dip._last_action = saved.get("last_action")
            dip._pending_buy_ord_id = saved.get("pending_buy") or None
            dip._pending_sell_ord_id = saved.get("pending_sell") or None
            if saved.get("batch_tp_triggered"):
                dip._batch_tp_triggered = set(saved["batch_tp_triggered"])
            logger.info(f"{inst_id} 从 Gist 恢复状态: pos={dip.position}")

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
        for inst_id in self.all_inst_ids():
            dip = self.dips.get(inst_id)
            if not dip:
                continue
            try:
                await dip._sync_position_from_okx()
                logger.info(f"{inst_id} 持仓已同步: {dip.position}")
            except Exception as e:
                logger.error(f"{inst_id} 持仓同步失败: {e}")

    async def start_periodic_save(self, interval=60):
        """每60秒自动保存一次到 Gist"""
        async def _loop():
            while True:
                await asyncio.sleep(interval)
                try:
                    await self.state_store.save_all(self)
                except Exception as e:
                    logger.error(f"定期保存失败: {e}")
        self._save_task = asyncio.create_task(_loop())
        logger.info("Gist 定期保存任务已启动（每60秒）")

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
            try:
                await self.state_store.save_all(self)
            except Exception as e:
                logger.error(f"停止后保存失败: {e}")
            return True, "已停止低吸高卖"
        return True, "低吸高卖未运行"