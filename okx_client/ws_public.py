import asyncio
import json
import logging
import time
import websockets
from config import OKX_DEMO

WS_PUBLIC = "wss://ws.okx.com:8443/ws/v5/public"
WS_PUBLIC_DEMO = "wss://wspap.okx.com:8443/ws/v5/public"

logger = logging.getLogger(__name__)


class PublicWS:
    def __init__(self, on_ticker):
        self.on_ticker = on_ticker
        self.ws = None
        self._inst_ids = set()
        self._running = False
        self.last_tick_time = time.time()

    async def connect(self, inst_ids: list):
        self._inst_ids = set(inst_ids)
        self._running = True
        url = WS_PUBLIC_DEMO if OKX_DEMO else WS_PUBLIC

        # 启动看门狗任务
        asyncio.create_task(self._watchdog())

        while self._running:
            try:
                self.ws = await websockets.connect(url, ping_interval=20, ping_timeout=10)
                if self._inst_ids:
                    await self._send_subscribe(list(self._inst_ids))
                logger.info("✅ 公共行情 WebSocket 已连接")
                self.last_tick_time = time.time()
                await self._loop()
            except Exception as e:
                logger.error(f"❌ 公共WS断开: {e}，5秒后重连...")
                await asyncio.sleep(5)

    async def _watchdog(self):
        """看门狗：监控行情流是否卡死"""
        while self._running:
            await asyncio.sleep(30)
            if time.time() - self.last_tick_time > 60:
                logger.warning("⚠️ 超过60秒未收到行情，强制重连 WebSocket...")
                if self.ws:
                    await self.ws.close()
                self.last_tick_time = time.time()

    async def _send_subscribe(self, inst_ids):
        args = [{"channel": "tickers", "instId": iid} for iid in inst_ids]
        await self.ws.send(json.dumps({"op": "subscribe", "args": args}))

    async def subscribe(self, inst_id):
        if inst_id in self._inst_ids:
            return
        self._inst_ids.add(inst_id)
        if self.ws:
            try:
                await self._send_subscribe([inst_id])
            except Exception:
                pass

    async def unsubscribe(self, inst_id):
        if inst_id not in self._inst_ids:
            return
        self._inst_ids.discard(inst_id)
        if self.ws:
            try:
                args = [{"channel": "tickers", "instId": inst_id}]
                await self.ws.send(json.dumps({"op": "unsubscribe", "args": args}))
            except Exception:
                pass

    async def _loop(self):
        async for raw in self.ws:
            msg = json.loads(raw)
            if msg.get("arg", {}).get("channel") == "tickers":
                for d in msg.get("data", []):
                    try:
                        inst_id = d["instId"]
                        price = float(d["last"])
                        self.last_tick_time = time.time() # 收到数据就刷新时间
                        await self.on_ticker(inst_id, price, d)
                    except Exception:
                        pass

    async def close(self):
        self._running = False
        if self.ws:
            await self.ws.close()