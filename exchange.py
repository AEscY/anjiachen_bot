import aiohttp
import asyncio
import json
from config import Config
from utils import okx_sign, get_timestamp

class OKXExchange:
    def __init__(self):
        self.base_url = "https://www.okx.com" if not Config.IS_SANDBOX else "https://www.okx.com" # 模拟盘亦用此域名，参数区分
        self.api_key = Config.OKX_API_KEY
        self.secret = Config.OKX_SECRET_KEY
        self.passphrase = Config.OKX_PASSPHRASE
        self.session = None

    async def _request(self, method, path, body=None):
        if self.session is None:
            self.session = aiohttp.ClientSession()
        
        timestamp = get_timestamp()
        body_str = json.dumps(body) if body else ""
        sign = okx_sign(timestamp, method, path, body_str, self.secret)
        
        headers = {
            "OKX-ACCESS-KEY": self.api_key,
            "OKX-ACCESS-SIGN": sign,
            "OKX-ACCESS-TIMESTAMP": timestamp,
            "OKX-ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type": "application/json"
        }
        
        url = self.base_url + path
        async with self.session.request(method, url, headers=headers, data=body_str) as resp:
            result = await resp.json()
            if result.get("code") != "0":
                print(f"❌ OKX API Error: {result}")
            return result

    async def place_order(self, side, price, size):
        """下单 (限价)"""
        path = "/api/v5/trade/order"
        body = {
            "instId": Config.SYMBOL,
            "tdMode": "cash",
            "side": side,
            "ordType": "limit",
            "px": str(price),
            "sz": str(size)
        }
        return await self._request("POST", path, body)

    async def cancel_order(self, order_id):
        path = "/api/v5/trade/cancel-order"
        body = {"instId": Config.SYMBOL, "ordId": order_id}
        return await self._request("POST", path, body)

    async def get_balance(self):
        path = "/api/v5/account/balance"
        return await self._request("GET", path)

    async def get_open_orders(self):
        path = "/api/v5/trade/orders-pending"
        body = {"instId": Config.SYMBOL}
        return await self._request("GET", path, body)