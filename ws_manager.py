"""
ws_manager.py - WebSocket 实时数据管理器（并发订阅多币种）
"""
import asyncio
import time
import ccxt.pro as ccxt_pro
from config import settings, logger


class WSDataManager:
    def __init__(self, exchange_rest):
        self.rest = exchange_rest
        self.exchange = None
        self.tickers = {}          # symbol -> {last, bid, ask, timestamp}
        self.orderbooks = {}       # symbol -> {bids, asks, timestamp}
        self._running = False
        self._lock = asyncio.Lock()

    async def connect(self):
        """建立 WebSocket 连接"""
        name = settings.EXCHANGE_NAME
        key = settings.OKX_API_KEY or settings.API_KEY
        secret = settings.OKX_SECRET_KEY or settings.SECRET_KEY
        password = settings.OKX_PASSPHRASE or settings.PASSWORD

        config = {'apiKey': key, 'secret': secret, 'enableRateLimit': True}
        if name in ('okx', 'bybit'):
            config['password'] = password

        exchange_class = getattr(ccxt_pro, name, None)
        if exchange_class is None:
            logger.error(f"❌ ccxt.pro 不支持 {name}")
            return False

        self.exchange = exchange_class(config)
        if settings.IS_SANDBOX and name == 'okx':
            self.exchange.set_sandbox_mode(True)

        logger.info(f"🔌 WebSocket 已连接到 {name}")
        return True

    async def watch_tickers(self, symbols):
        """批量订阅所有币种价格（使用 watch_tickers 并发）"""
        self._running = True
        while self._running:
            try:
                # 一次订阅所有币种，CCXT 内部复用 WebSocket 连接
                tickers = await self.exchange.watch_tickers(symbols)
                if tickers:
                    async with self._lock:
                        for symbol, ticker in tickers.items():
                            if ticker and 'symbol' in ticker:
                                self.tickers[ticker['symbol']] = {
                                    'last': ticker.get('last', 0),
                                    'bid': ticker.get('bid', 0),
                                    'ask': ticker.get('ask', 0),
                                    'timestamp': time.time()
                                }
            except Exception as e:
                logger.warning(f"WebSocket 批量订阅断线: {e}")
                await asyncio.sleep(1)

    async def watch_orderbooks(self, symbols, limit=5):
        """批量订阅订单簿（使用 watch_order_books）"""
        while self._running:
            try:
                # 批量订阅所有币种订单簿
                orderbooks = await self.exchange.watch_order_books(symbols, limit)
                if orderbooks:
                    async with self._lock:
                        for symbol, ob in orderbooks.items():
                            if ob and 'symbol' in ob:
                                self.orderbooks[ob['symbol']] = {
                                    'bids': ob.get('bids', []),
                                    'asks': ob.get('asks', []),
                                    'timestamp': time.time()
                                }
            except Exception as e:
                logger.warning(f"WebSocket 订单簿批量订阅断线: {e}")
                await asyncio.sleep(1)

    def get_ticker(self, symbol):
        """获取缓存的最新价格（非阻塞）"""
        return self.tickers.get(symbol)

    def get_orderbook(self, symbol):
        """获取缓存的最新订单簿（非阻塞）"""
        return self.orderbooks.get(symbol)

    def get_last_price(self, symbol):
        """获取最新成交价"""
        ticker = self.tickers.get(symbol)
        if ticker:
            return ticker['last']
        return None

    async def stop(self):
        self._running = False
        if self.exchange:
            await self.exchange.close()