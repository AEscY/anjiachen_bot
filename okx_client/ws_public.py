# okx_client/ws_public.py
import json, asyncio, hmac, hashlib, base64, time
import websockets
from config import OKX_API_KEY, OKX_SECRET_KEY, OKX_PASSPHRASE, OKX_DEMO

WS_PUBLIC = "wss://ws.okx.com:8443/ws/v5/public"
WS_PUBLIC_DEMO = "wss://wspap.okx.com:8443/ws/v5/public"

class PublicWS:
    def __init__(self, on_ticker):
        self.on_ticker = on_ticker
        self.ws = None

    async def connect(self, inst_id: str):
        url = WS_PUBLIC_DEMO if OKX_DEMO else WS_PUBLIC
        self.ws = await websockets.connect(url, ping_interval=20)
        await self.ws.send(json.dumps({
            "op": "subscribe",
            "args": [{"channel": "tickers", "instId": inst_id}]
        }))
        asyncio.create_task(self._loop())

    async def _loop(self):
        async for raw in self.ws:
            msg = json.loads(raw)
            if msg.get("arg", {}).get("channel") == "tickers":
                for d in msg.get("data", []):
                    await self.on_ticker(float(d["last"]), d)

    async def close(self):
        if self.ws:
            await self.ws.close()