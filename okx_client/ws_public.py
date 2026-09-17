import json
import asyncio
import logging
import websockets
from config import OKX_DEMO

logger = logging.getLogger(__name__)

WS_PUBLIC = "wss://ws.okx.com:8443/ws/v5/public"
WS_PUBLIC_DEMO = "wss://wspap.okx.com:8443/ws/v5/public"


class PublicWS:
    def __init__(self, on_ticker):
        self.on_ticker = on_ticker
        self.ws = None
        self._inst_ids = set()

    async def connect(self, inst_ids: list):
        self._inst_ids.update(inst_ids)
        url = WS_PUBLIC_DEMO if OKX_DEMO else WS_PUBLIC

        # 死循环，断开后立即重连
        while True:
            try:
                logger.info(f"正在连接公共WebSocket...")
                self.ws = await websockets.connect(url, ping_interval=20)
                args = [{"channel": "tickers", "instId": iid} for iid in self._inst_ids]
                if args:
                    await self.ws.send(json.dumps({"op": "subscribe", "args": args}))
                logger.info(f"公共WebSocket已连接，订阅: {list(self._inst_ids)}")
                await self._loop()
            except Exception as e:
                logger.error(f"公共WebSocket断开或异常: {e}，5秒后重连...")
                await asyncio.sleep(5)

    async def _loop(self):
        async for raw in self.ws:
            msg = json.loads(raw)
            if msg.get("arg", {}).get("channel") == "tickers":
                for d in msg.get("data", []):
                    try:
                        inst_id = d["instId"]
                        price = float(d["last"])
                        await self.on_ticker(inst_id, price, d)
                    except Exception:
                        pass

    async def subscribe(self, inst_id):
        if inst_id in self._inst_ids:
            return
        self._inst_ids.add(inst_id)
        if self.ws:
            try:
                await self.ws.send(json.dumps({
                    "op": "subscribe",
                    "args": [{"channel": "tickers", "instId": inst_id}]
                }))
            except Exception as e:
                logger.error(f"订阅 {inst_id} 失败: {e}")

    async def unsubscribe(self, inst_id):
        if inst_id not in self._inst_ids:
            return
        self._inst_ids.discard(inst_id)
        if self.ws:
            try:
                await self.ws.send(json.dumps({
                    "op": "unsubscribe",
                    "args": [{"channel": "tickers", "instId": inst_id}]
                }))
            except Exception as e:
                logger.error(f"退订 {inst_id} 失败: {e}")

    async def close(self):
        if self.ws:
            await self.ws.close()