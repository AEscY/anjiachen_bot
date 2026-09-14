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

    async def _check_min_order(self, inst_id, price, spend_usdt):
        """校验单次投入是否满足 OKX 最小下单要求"""
        try:
            resp = await asyncio.to_thread(self.rest.get_instruments, "SPOT", inst_id)
            if resp.get("code") != "0" or not resp.get("data"):
                return False, None, "无法获取交易规则"
            inst = resp["data"][0]
            min_sz = float(inst.get("minSz", 0))
            lot_sz = float(inst.get("lotSz", 0.00000001))
            min_notional = min_sz * price
            if spend_usdt < min_notional:
                return False, None, (
                    f"投入 {spend_usdt:.2f} USDT 低于 {inst_id} 最低要求 "
                    f"{min_notional:.2f} USDT (minSz={min_sz})"
                )
            raw_qty = spend_usdt / price
            aligned_qty = int(raw_qty / lot_sz) * lot_sz
            if aligned_qty < min_sz:
                return False, None, (
                    f"可买入数量 {aligned_qty:.8f} 低于最小交易数量 {min_sz}"
                )
            return True, aligned_qty, f"OK, 可买入 {aligned_qty:.8f}"
        except Exception as e:
            return False, None, f"校验失败: {e}"

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

        # 校验默认投入是否满足最小下单要求
        ok, _, msg = await self._check_min_order(inst_id, price, DIP_MAX_SPEND)
        if not ok:
            logger.warning(f"{inst_id} 最小下单校验: {msg}")

        self.grids[inst_id] = GridStrategy(inst_id, {"quoteSz": GRID_QUOTE_SZ})
        self.dips[inst_id] = DipSellStrategy(inst_id, {"maxSpend": DIP_MAX_SPEND})
        self._inst_ids.append(inst_id)
        logger.info(f"已添加 {inst_id} @ {price}")
        return True, f"已添加 {inst_id}，当前价 {price}\n{msg}"

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

    async def _calc_atr(self, inst_id, period=14):
        try:
            resp = await asyncio.to_thread(self.rest.get_candles, inst_id, "1H", period + 1)
            if resp.get("code") != "0" or not resp.get("data"):
                return None
            candles = list(reversed(resp["data"]))
            if len(candles) < 2:
                return None
            trs = []
            for i in range(1, len(candles)):
                high = float(candles[i][2])
                low = float(candles[i][3])
                prev_close = float(candles[i - 1][4])
                tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
                trs.append(tr)
            return sum(trs) / len(trs) if trs else None
        except Exception as e:
            logger.error(f"计算 {inst_id} ATR 失败: {e}")
            return None

    async def get_recommend_params(self, inst_id):
        try:
            resp = await asyncio.to_thread(self.rest.get_ticker, inst_id)
            if resp.get("code") != "0" or not resp.get("data"):
                return None
            price = float(resp["data"][0]["last"])
        except Exception as e:
            logger.error(f"获取 {inst_id} 价格失败: {e}")
            return None

        atr = await self._calc_atr(inst_id)
        if atr is None:
            atr = price * 0.015

        range_pct = 0.15
        grid_num = 20
        min_px = round(price * (1 - range_pct), 6)
        max_px = round(price * (1 + range_pct), 6)
        buy_px = round(price - 1.5 * atr, 6)
        sell_px = round(price + 1.5 * atr, 6)

        # 同时获取最小下单信息
        min_order_info = ""
        try:
            ok, qty, msg = await self._check_min_order(inst_id, price, DIP_MAX_SPEND)
            min_order_info = msg
        except Exception:
            pass

        return {
            "inst_id": inst_id,
            "price": price,
            "atr": atr,
            "grid": {"minPx": min_px, "maxPx": max_px, "gridNum": grid_num},
            "dip": {"buyPx": buy_px, "sellPx": sell_px},
            "min_order_info": min_order_info,
        }