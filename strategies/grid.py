import asyncio
import logging
from strategies.base import BaseStrategy
from okx_client.rest import OKXRest

logger = logging.getLogger(__name__)


class GridStrategy(BaseStrategy):
    def __init__(self, inst_id, params=None):
        default = {
            "minPx": 0,
            "maxPx": 0,
            "gridNum": 20,
            "quoteSz": 100,
            "auto_mode": True,
            "rebuild_threshold": 0.03,
        }
        merged = {**default, **(params or {})}
        super().__init__(inst_id, merged)
        self.rest = OKXRest()
        self.algo_id = None
        self._monitor_task = None
        self._last_price = 0.0
        self.rebuild_count = 0
        self.min_investment = 0.0

    async def start(self):
        if self.running:
            return
        if self.params.get("auto_mode", True):
            await self._calc_auto_params()

        # 校验最小投资
        ok, msg = await self._check_min_investment()
        if not ok:
            raise RuntimeError(msg)

        await self._create_grid()

    async def _check_min_investment(self):
        """通过 OKX 接口查询最小投资额,不足时提示"""
        try:
            resp = await asyncio.to_thread(
                self.rest.get_min_investment,
                self.inst_id, "grid",
                self.params["minPx"],
                self.params["maxPx"],
                self.params["gridNum"],
            )
            if resp.get("code") == "0" and resp.get("data"):
                min_inv = float(resp["data"][0].get("minInvestment", 0))
                self.min_investment = min_inv
                quote_sz = float(self.params.get("quoteSz", 100))
                if quote_sz < min_inv:
                    # 自动提升到最小值
                    self.params["quoteSz"] = min_inv
                    logger.info(f"{self.inst_id} 网格投资额 {quote_sz} 不足,已自动提升至 {min_inv}")
                return True, ""
            else:
                # 接口失败时降级校验
                return await self._fallback_check()
        except Exception as e:
            logger.error(f"{self.inst_id} 查询最小投资失败: {e}")
            return await self._fallback_check()

    async def _fallback_check(self):
        """降级校验:用交易产品规格估算"""
        try:
            resp = await asyncio.to_thread(self.rest.get_instruments, "SPOT", self.inst_id)
            if resp.get("code") != "0" or not resp.get("data"):
                return True, ""
            inst = resp["data"][0]
            min_sz = float(inst.get("minSz", 0))
            ticker = await asyncio.to_thread(self.rest.get_ticker, self.inst_id)
            price = float(ticker["data"][0]["last"])
            min_notional = min_sz * price
            # 网格一般要求单格金额 > 最小订单
            grid_num = self.params["gridNum"]
            quote_sz = float(self.params.get("quoteSz", 100))
            per_grid = quote_sz / grid_num if grid_num > 0 else 0
            if per_grid < min_notional:
                needed = min_notional * grid_num * 1.5
                self.params["quoteSz"] = round(needed, 2)
                logger.info(f"{self.inst_id} 每格 {per_grid:.4f} < 最小 {min_notional:.4f},投资额提升至 {needed:.2f}")
            return True, ""
        except Exception as e:
            logger.error(f"{self.inst_id} 降级校验失败: {e}")
            return True, ""

    async def _calc_auto_params(self):
        resp = await asyncio.to_thread(self.rest.get_ticker, self.inst_id)
        if resp.get("code") != "0" or not resp.get("data"):
            raise RuntimeError("获取行情失败")
        price = float(resp["data"][0]["last"])

        atr = await self._calc_atr()
        if atr is None:
            atr = price * 0.015

        range_pct = max(0.15, 2.5 * atr / price)
        self.params["minPx"] = round(price * (1 - range_pct), 6)
        self.params["maxPx"] = round(price * (1 + range_pct), 6)

        span = self.params["maxPx"] - self.params["minPx"]
        grid_num = int(span / price / 0.008)
        grid_num = max(10, min(60, grid_num))
        self.params["gridNum"] = grid_num

        logger.info(
            f"{self.inst_id} 自动参数: 区间 {self.params['minPx']}-{self.params['maxPx']}, "
            f"网格数 {grid_num}, ATR {atr:.2f}"
        )

    async def _calc_atr(self, period=14):
        try:
            resp = await asyncio.to_thread(self.rest.get_candles, self.inst_id, "1H", period + 1)
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
            logger.error(f"{self.inst_id} 计算ATR失败: {e}")
            return None

    async def _create_grid(self):
        r = await asyncio.to_thread(
            self.rest.create_spot_grid,
            self.inst_id,
            self.params["minPx"],
            self.params["maxPx"],
            self.params["gridNum"],
            self.params.get("quoteSz", 100),
        )
        data = r.get("data", [])
        if not data:
            raise RuntimeError(f"网格创建失败: {r}")
        self.algo_id = data[0]["algoId"]
        self.running = True
        logger.info(f"{self.inst_id} 网格已启动: {self.algo_id}")

        if self._monitor_task is None or self._monitor_task.done():
            self._monitor_task = asyncio.create_task(self._monitor())

    async def _monitor(self):
        while self.running:
            await asyncio.sleep(300)
            if not self.running:
                break
            try:
                price = self._last_price
                if price <= 0:
                    continue
                threshold = self.params.get("rebuild_threshold", 0.03)
                min_px = self.params["minPx"]
                max_px = self.params["maxPx"]
                if price < min_px * (1 + threshold) or price > max_px * (1 - threshold):
                    logger.info(f"{self.inst_id} 价格 {price} 接近边界,自动重建网格")
                    await self._rebuild()
            except Exception as e:
                logger.error(f"{self.inst_id} 监控失败: {e}")

    async def _rebuild(self):
        old_algo = self.algo_id
        try:
            if old_algo:
                await asyncio.to_thread(self.rest.stop_grid, old_algo, self.inst_id)
        except Exception as e:
            logger.error(f"停止旧网格失败: {e}")
        self.running = False
        await asyncio.sleep(3)
        try:
            await self._calc_auto_params()
            ok, msg = await self._check_min_investment()
            if not ok:
                logger.error(f"{self.inst_id} 重建失败: {msg}")
                return
            await self._create_grid()
            self.rebuild_count += 1
            logger.info(f"{self.inst_id} 网格已自动重建 #{self.rebuild_count}")
        except Exception as e:
            logger.error(f"{self.inst_id} 网格重建失败: {e}")

    async def on_ticker(self, price, raw):
        self._last_price = price

    async def stop(self):
        self.running = False
        if self._monitor_task:
            self._monitor_task.cancel()
            self._monitor_task = None
        if self.algo_id:
            try:
                await asyncio.to_thread(self.rest.stop_grid, self.algo_id, self.inst_id)
            except Exception as e:
                logger.error(f"停止网格失败: {e}")
            self.algo_id = None

    def snapshot(self):
        s = super().snapshot()
        s.update({
            "algo_id": self.algo_id,
            "rebuild_count": self.rebuild_count,
            "min_investment": self.min_investment,
        })
        return s