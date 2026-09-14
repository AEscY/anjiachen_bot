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
            "take_profit_pct": 0.05,
            "stop_loss_pct": 0.08,
        }
        merged = {**default, **(params or {})}
        super().__init__(inst_id, merged)
        self.rest = OKXRest()
        self.algo_id = None
        self._last_price = 0.0
        self.min_investment = 0.0

    async def start(self):
        if self.running:
            return
        if self.params.get("auto_mode", True):
            await self._calc_auto_params()

        ok, msg = await self._check_min_investment()
        if not ok:
            raise RuntimeError(msg)

        await self._create_grid()

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

        # 止盈止损价
        self.params["tp_px"] = round(price * (1 + self.params.get("take_profit_pct", 0.05)), 6)
        self.params["sl_px"] = round(price * (1 - self.params.get("stop_loss_pct", 0.08)), 6)

        logger.info(
            f"{self.inst_id} 自动参数: 区间 {self.params['minPx']}-{self.params['maxPx']}, "
            f"网格数 {grid_num}, 止盈 {self.params['tp_px']}, 止损 {self.params['sl_px']}"
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

    async def _check_min_investment(self):
        try:
            resp = await asyncio.to_thread(
                self.rest.get_min_investment,
                self.inst_id, "grid",
                self.params["minPx"], self.params["maxPx"], self.params["gridNum"],
            )
            if resp.get("code") == "0" and resp.get("data"):
                min_inv = float(resp["data"][0].get("minInvestment", 0))
                self.min_investment = min_inv
                quote_sz = float(self.params.get("quoteSz", 100))
                if quote_sz < min_inv:
                    self.params["quoteSz"] = min_inv
                    logger.info(f"{self.inst_id} 投资额已自动提升至 {min_inv}")
                return True, ""
            return await self._fallback_check()
        except Exception as e:
            logger.error(f"{self.inst_id} 查询最小投资失败: {e}")
            return await self._fallback_check()

    async def _fallback_check(self):
        try:
            resp = await asyncio.to_thread(self.rest.get_instruments, "SPOT", self.inst_id)
            if resp.get("code") != "0" or not resp.get("data"):
                return True, ""
            inst = resp["data"][0]
            min_sz = float(inst.get("minSz", 0))
            ticker = await asyncio.to_thread(self.rest.get_ticker, self.inst_id)
            price = float(ticker["data"][0]["last"])
            min_notional = min_sz * price
            grid_num = self.params["gridNum"]
            quote_sz = float(self.params.get("quoteSz", 100))
            per_grid = quote_sz / grid_num if grid_num > 0 else 0
            if per_grid < min_notional:
                needed = min_notional * grid_num * 1.5
                self.params["quoteSz"] = round(needed, 2)
                logger.info(f"{self.inst_id} 每格不足，投资额提升至 {needed:.2f}")
            return True, ""
        except Exception as e:
            logger.error(f"{self.inst_id} 降级校验失败: {e}")
            return True, ""

    async def _create_grid(self):
        r = await asyncio.to_thread(
            self.rest.create_spot_grid,
            self.inst_id,
            self.params["minPx"],
            self.params["maxPx"],
            self.params["gridNum"],
            self.params.get("quoteSz", 100),
            self.params.get("tp_px"),
            self.params.get("sl_px"),
        )
        data = r.get("data", [])
        if not data:
            raise RuntimeError(f"网格创建失败: {r}")
        self.algo_id = data[0]["algoId"]
        self.running = True
        logger.info(f"{self.inst_id} 网格已启动: {self.algo_id}")

    async def on_ticker(self, price, raw):
        self._last_price = price

    async def stop(self):
        self.running = False
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
            "min_investment": self.min_investment,
            "tp_px": self.params.get("tp_px"),
            "sl_px": self.params.get("sl_px"),
        })
        return s