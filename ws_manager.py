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
        self.tickers = {}
        self.orderbooks = {}
        self._running = False
        self._lock = asyncio.Lock()
        self._reconnect_attempt = 0

    async def connect(self):
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

        if self.exchange:
            try:
                await self.exchange.close()
            except:
                pass

        self.exchange = exchange_class(config)
        if settings.IS_SANDBOX and name == 'okx':
            self.exchange.set_sandbox_mode(True)

        logger.info(f"🔌 WebSocket 已连接到 {name}")
        return True

    async def watch_tickers(self, symbols):
        self._running = True
        while self._running:
            try:
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
                self._reconnect_attempt = 0  # 成功后重置重试计数
            except Exception as e:
                logger.warning(f"WebSocket 批量订阅断线: {e}")
                self._reconnect_attempt += 1
                # 指数退避，最大60秒
                wait = min(60, 2 ** self._reconnect_attempt)
                logger.info(f"🔄 等待 {wait}s 后重连...")
                await asyncio.sleep(wait)
                # 重新初始化连接
                await self.connect()
                # 重新订阅
                if self.exchange:
                    try:
                        await self.exchange.watch_tickers(symbols)
                    except:
                        pass

    async def watch_orderbooks(self, symbols, limit=5):
        while self._running:
            try:
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
                self._reconnect_attempt = 0
            except Exception as e:
                logger.warning(f"WebSocket 订单簿批量订阅断线: {e}")
                self._reconnect_attempt += 1
                wait = min(60, 2 ** self._reconnect_attempt)
                await asyncio.sleep(wait)
                await self.connect()
                if self.exchange:
                    try:
                        await self.exchange.watch_order_books(symbols, limit)
                    except:
                        pass

    def get_ticker(self, symbol):
        async with self._lock:
            return self.tickers.get(symbol)

    def get_orderbook(self, symbol):
        async with self._lock:
            return self.orderbooks.get(symbol)

    def get_last_price(self, symbol):
        ticker = self.get_ticker(symbol)
        if ticker:
            return ticker['last']
        return None

    async def stop(self):
        self._running = False
        if self.exchange:
            await self.exchange.close()