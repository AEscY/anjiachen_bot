import asyncio
import websockets
import json
import threading
from config import Config
from utils import calculate_imbalance

class WSManager:
    def __init__(self, on_update_callback):
        self.url = Config.WS_URL
        self.callback = on_update_callback
        self.ws = None
        self.last_price = 0.0
        self.bids = []   # 买盘
        self.asks = []   # 卖盘
        self.imbalance = 0.0
        self._running = False

    async def connect(self):
        self._running = True
        while self._running:
            try:
                async with websockets.connect(self.url) as websocket:
                    self.ws = websocket
                    # 订阅交易对深度和Ticker
                    sub_depth = {
                        "op": "subscribe",
                        "args": [{"channel": "books", "instId": Config.SYMBOL}]
                    }
                    sub_ticker = {
                        "op": "subscribe",
                        "args": [{"channel": "tickers", "instId": Config.SYMBOL}]
                    }
                    await websocket.send(json.dumps(sub_depth))
                    await websocket.send(json.dumps(sub_ticker))
                    print(f"✅ WebSocket 已连接，监听 {Config.SYMBOL}")
                    
                    async for message in websocket:
                        await self._handle_message(json.loads(message))
            except Exception as e:
                print(f"⚠️ WebSocket断开，重连中... {e}")
                await asyncio.sleep(3)

    async def _handle_message(self, data):
        if "data" not in data:
            return
        
        for item in data["data"]:
            # 处理深度数据
            if "bids" in item and "asks" in item:
                self.bids = item["bids"]
                self.asks = item["asks"]
                self.imbalance = calculate_imbalance(self.bids, self.asks, depth=15)
                # 触发策略回调
                if self.callback:
                    await self.callback("depth", self)
            
            # 处理最新价
            if "last" in item:
                self.last_price = float(item["last"])
                if self.callback:
                    await self.callback("ticker", self)

    def stop(self):
        self._running = False