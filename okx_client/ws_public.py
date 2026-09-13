# okx_client/ws_public.py
import json
import asyncio
import websockets
from config import OKX_DEMO

WS_PUBLIC      = "wss://ws.okx.com:8443/ws/v5/public"
WS_PUBLIC_DEMO = "wss://wspap.okx.com:8443/ws/v5/public"


class PublicWS:
    def __init__(self, on_ticker):
        """on_ticker(inst_id: str, price: float, raw: dict)"""
        self.on_ticker = on_ticker
        self.ws = None
        self._inst_ids = set()

    async def connect(self, inst_ids: list):
        url = WS_PUBLIC_DEMO if OKX_DEMO else WS_PUBLIC
        self.ws = await websockets.connect(url, ping_interval=20)
        if inst_ids:
            await self._send_subscribe(inst_ids)
            self._inst_ids.update(inst_ids)
        asyncio.create_task(self._loop())

    async def _send_subscribe(self, inst_ids):
        args = [{"channel": "tickers", "instId": iid} for iid in inst_ids]
        await self.ws.send(json.dumps({"op": "subscribe", "args": args}))

    async def subscribe(self, inst_id):
        if inst_id in self._inst_ids or not self.ws:
            return
        await self._send_subscribe([inst_id])
        self._inst_ids.add(inst_id)

    async def unsubscribe(self, inst_id):
        if inst_id not in self._inst_ids or not self.ws:
            return
        args = [{"channel": "tickers", "instId": inst_id}]
        await self.ws.send(json.dumps({"op": "unsubscribe", "args": args}))
        self._inst_ids.discard(inst_id)

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
        if self.ws:
            await self.ws.close()