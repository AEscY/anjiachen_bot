# strategies/grid.py
import asyncio
import logging
from strategies.base import BaseStrategy
from okx_client.rest import OKXRest

logger = logging.getLogger(__name__)


class GridStrategy(BaseStrategy):
    def __init__(self, inst_id: str, params: dict | None = None):
        # 默认参数（只在未传时使用）
        default = {
            "minPx": 0,
            "maxPx": 0,
            "gridNum": 20,
            "quoteSz": 100,
        }
        merged = {**default, **(params or {})}
        super().__init__(inst_id, merged)
        self.rest = OKXRest()
        self.algo_id = None
        self._monitor_task = None

    async def start(self):
        if self.running:
            return
        r = self.rest.create_spot_grid(
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
        self._monitor_task = asyncio.create_task(self._monitor())
        logger.info(f"{self.inst_id} 网格已启动: {self.algo_id}")

    async def _monitor(self):
        while self.running:
            await asyncio.sleep(60)
            # 后续可加边界重建逻辑

    async def on_ticker(self, price: float, raw: dict):
        # 网格由 OKX 服务端执行，此处留空给未来扩展
        pass

    async def stop(self):
        self.running = False
        if self._monitor_task:
            self._monitor_task.cancel()
            self._monitor_task = None
        if self.algo_id:
            try:
                self.rest.stop_grid(self.algo_id, self.inst_id)
            except Exception as e:
                logger.error(f"停止网格失败: {e}")
            self.algo_id = None

    def snapshot(self):
        s = super().snapshot()
        s["algo_id"] = self.algo_id
        return s