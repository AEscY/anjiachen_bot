import asyncio
import json
import logging
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

    async def connect(self, inst_ids: list):
        self._inst_ids = set(inst_ids)
        self._running = True
        url = WS_PUBLIC_DEMO if OKX_DEMO else WS_PUBLIC

        # ===== 新增：无限重连循环 =====
        while self._running:
            try:
                self.ws = await websockets.connect(url, ping_interval=20, ping_timeout=10)
                if self._inst_ids:
                    await self._send_subscribe(list(self._inst_ids))
                logger.info("✅ 公共行情 WebSocket 已连接")
                await self._loop()
            except Exception as e:
                logger.error(f"❌ 公共WS断开: {e}，5秒后重连...")
                await asyncio.sleep(5)

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
                        await self.on_ticker(inst_id, price, d)
                    except Exception:
                        pass

    async def close(self):
        self._running = False
        if self.ws:
            await self.ws.close()