import json
import asyncio
import hmac
import hashlib
import base64
import time
import websockets
from config import OKX_API_KEY, OKX_SECRET_KEY, OKX_PASSPHRASE, OKX_DEMO

WS_BUSINESS = "wss://ws.okx.com:8443/ws/v5/business"
WS_BUSINESS_DEMO = "wss://wspap.okx.com:8443/ws/v5/business?brokerId=9999"

def _sign(ts: str) -> str:
    msg = ts + "GET" + "/users/self/verify"
    mac = hmac.new(
        OKX_SECRET_KEY.encode(),
        msg.encode(),
        hashlib.sha256
    )
    return base64.b64encode(mac.digest()).decode()

class BusinessWS:
    """网格子订单WebSocket"""

    def __init__(self, on_sub_order):
        self.on_sub_order = on_sub_order
        self.ws = None
        self._subscribed_algo_ids = set()

    async def connect(self, algo_ids: list):
        url = WS_BUSINESS_DEMO if OKX_DEMO else WS_BUSINESS
        self.ws = await websockets.connect(url, ping_interval=20)

        # 使用秒级时间戳（OKX要求）
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
            raise RuntimeError(f"OKX business login failed: {resp}")

        for algo_id in algo_ids:
            await self.subscribe(algo_id)

        asyncio.create_task(self._loop())

    async def subscribe(self, algo_id: str):
        if algo_id in self._subscribed_algo_ids or not self.ws:
            return
        await self.ws.send(json.dumps({
            "op": "subscribe",
            "args": [{"channel": "grid-sub-orders", "algoId": algo_id}]
        }))
        self._subscribed_algo_ids.add(algo_id)

    async def unsubscribe(self, algo_id: str):
        if algo_id not in self._subscribed_algo_ids or not self.ws:
            return
        await self.ws.send(json.dumps({
            "op": "unsubscribe",
            "args": [{"channel": "grid-sub-orders", "algoId": algo_id}]
        }))
        self._subscribed_algo_ids.discard(algo_id)

    async def _loop(self):
        async for raw in self.ws:
            msg = json.loads(raw)
            if msg.get("arg", {}).get("channel") == "grid-sub-orders":
                for d in msg.get("data", []):
                    await self.on_sub_order(d)

    async def close(self):
        if self.ws:
            await self.ws.close()