# okx_client/ws_private.py
import json, asyncio, hmac, hashlib, base64, time
import websockets
from config import OKX_API_KEY, OKX_SECRET_KEY, OKX_PASSPHRASE, OKX_DEMO

WS_PRIVATE = "wss://ws.okx.com:8443/ws/v5/private"
WS_PRIVATE_DEMO = "wss://wspap.okx.com:8443/ws/v5/private?brokerId=9999"

def _sign(ts: str) -> str:
    msg = ts + "GET" + "/users/self/verify"
    mac = hmac.new(OKX_SECRET_KEY.encode(), msg.encode(), hashlib.sha256)
    return base64.b64encode(mac.digest()).decode()

class PrivateWS:
    def __init__(self, on_order_update):
        self.on_order_update = on_order_update
        self.ws = None

    async def connect(self):
        url = WS_PRIVATE_DEMO if OKX_DEMO else WS_PRIVATE
        self.ws = await websockets.connect(url, ping_interval=20)
        ts = str(int(time.time()))
        await self.ws.send(json.dumps({
            "op": "login",
            "args": [{
                "apiKey": OKX_API_KEY,
                "passphrase": OKX_PASSPHRASE,
                "timestamp": ts,
                "sign": _sign(ts)
            }]
        }))
        resp = json.loads(await self.ws.recv())
        if resp.get("code") != "0":
            raise RuntimeError(f"OKX login failed: {resp}")
        await self.ws.send(json.dumps({
            "op": "subscribe",
            "args": [{"channel": "orders", "instType": "SPOT"}]
        }))
        asyncio.create_task(self._loop())

    async def _loop(self):
        async for raw in self.ws:
            msg = json.loads(raw)
            if msg.get("arg", {}).get("channel") == "orders":
                for d in msg.get("data", []):
                    await self.on_order_update(d)

    async def place_order(self, payload: dict):
        await self.ws.send(json.dumps({
            "op": "order",
            "args": [payload]
        }))

    async def close(self):
        if self.ws:
            await self.ws.close()