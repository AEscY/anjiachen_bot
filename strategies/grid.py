# strategies/grid.py
import asyncio
from strategies.base import BaseStrategy
from okx_client.rest import OKXRest
from config import GRID_DEFAULT

class GridStrategy(BaseStrategy):
    def __init__(self, inst_id: str, params: dict | None = None):
        super().__init__(inst_id, {**GRID_DEFAULT, **(params or {})})
        self.rest = OKXRest()
        self.algo_id = None
        self._monitor_task = None

    async def start(self):
        r = self.rest.create_spot_grid(
            self.inst_id,
            self.params["minPx"], self.params["maxPx"],
            self.params["gridNum"], self.params.get("quoteSz", 100)
        )
        data = r.get("data", [])
        if not data:
            raise RuntimeError(f"网格创建失败: {r}")
        self.algo_id = data[0]["algoId"]
        self.running = True
        self._monitor_task = asyncio.create_task(self._monitor())

    async def _monitor(self):
        while self.running:
            await asyncio.sleep(60)
            # 此处可插入边界监测：若价格偏离区间，停止旧网格并在新区间重建

    async def on_ticker(self, price: float, raw: dict):
        # 网格由 OKX 服务端执行，此处仅接收行情用于边界监测
        pass

    async def stop(self):
        self.running = False
        if self._monitor_task:
            self._monitor_task.cancel()
        if self.algo_id:
            self.rest.stop_grid(self.algo_id, self.inst_id)
            self.algo_id = None

    def snapshot(self):
        s = super().snapshot()
        s["algo_id"] = self.algo_id
        return s