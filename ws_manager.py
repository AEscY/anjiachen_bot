"""
ws_manager.py - WebSocket 实时数据管理器（兼容 OKX 单个订阅）
"""
import asyncio
import time
try:
    import ccxt.pro as ccxt_pro
except ImportError:
    ccxt_pro = None
from config import settings, logger


class WSDataManager:
    def __init__(self, exchange_rest):
        self.rest = exchange_rest
        self.exchange = None
        self.tickers = {}
        self.orderbooks = {}
        self._running = False
        self._lock = asyncio.Lock()
        self._reconnect_attempts = {}
        self._connect_lock = asyncio.Lock()
        self._orderbook_tasks = []

    async def connect(self):
        async with self._connect_lock:
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
        if self.exchange is None:
            while self._running:
                try:
                    results = await asyncio.gather(*(self.rest.fetch_ticker(sym) for sym in symbols), return_exceptions=True)
                    async with self._lock:
                        for sym, ticker in zip(symbols, results):
                            if isinstance(ticker, dict) and ticker.get('last') is not None:
                                self.tickers[sym] = {
                                    'last': ticker.get('last', 0), 'bid': ticker.get('bid', 0),
                                    'ask': ticker.get('ask', 0), 'percentage': ticker.get('percentage', 0),
                                    'volume': ticker.get('baseVolume', 0), 'timestamp': time.time()
                                }
                    await asyncio.sleep(2)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning(f'REST ticker 轮询异常: {e}')
                    await asyncio.sleep(5)
            return
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
                                    'percentage': ticker.get('percentage', 0),
                                    'volume': ticker.get('baseVolume', 0),
                                    'timestamp': time.time()
                                }
                self._reconnect_attempts['ticker'] = 0
            except Exception as e:
                logger.warning(f"Ticker WebSocket 断线: {e}")
                self._reconnect_attempts['ticker'] = self._reconnect_attempts.get('ticker', 0) + 1
                attempt = self._reconnect_attempts['ticker']
                wait = min(60, 2 ** min(attempt, 6))
                logger.info(f"🔄 等待 {wait}s 后重连 Ticker...")
                await asyncio.sleep(wait)
                # 不关闭共享 exchange；ccxt.pro 的 watch_* 会在底层连接断开后自动重连。

    async def _watch_single_orderbook(self, symbol, limit=5):
        if self.exchange is None:
            while self._running:
                try:
                    orderbook = await self.rest.fetch_orderbook(symbol, limit)
                    if orderbook:
                        async with self._lock:
                            self.orderbooks[symbol] = {
                                'bids': orderbook.get('bids', []), 'asks': orderbook.get('asks', []),
                                'timestamp': time.time()
                            }
                    await asyncio.sleep(3)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning(f'REST 订单簿 {symbol} 轮询异常: {e}')
                    await asyncio.sleep(5)
            return
        while self._running:
            try:
                orderbook = await self.exchange.watch_order_book(symbol, limit)
                if orderbook and 'symbol' in orderbook:
                    async with self._lock:
                        self.orderbooks[orderbook['symbol']] = {
                            'bids': orderbook.get('bids', []),
                            'asks': orderbook.get('asks', []),
                            'timestamp': time.time()
                        }
                self._reconnect_attempts[symbol] = 0
            except Exception as e:
                logger.warning(f"订单簿 {symbol} WebSocket 断线: {e}")
                self._reconnect_attempts[symbol] = self._reconnect_attempts.get(symbol, 0) + 1
                attempt = self._reconnect_attempts[symbol]
                wait = min(60, 2 ** min(attempt, 6))
                logger.info(f"🔄 订单簿 {symbol} 等待 {wait}s 后重连...")
                await asyncio.sleep(wait)
                # 不重建共享 exchange，避免影响 ticker 任务。

    async def watch_orderbooks(self, symbols, limit=5):
        for task in self._orderbook_tasks:
            if not task.done():
                task.cancel()
        self._orderbook_tasks.clear()

        for symbol in symbols:
            task = asyncio.create_task(self._watch_single_orderbook(symbol, limit))
            self._orderbook_tasks.append(task)

        await asyncio.gather(*self._orderbook_tasks, return_exceptions=True)

    def get_ticker(self, symbol):
        return self.tickers.get(symbol)

    def get_orderbook(self, symbol):
        return self.orderbooks.get(symbol)

    def get_last_price(self, symbol):
        ticker = self.get_ticker(symbol)
        if ticker:
            return ticker['last']
        return None

    async def stop(self):
        self._running = False
        for task in self._orderbook_tasks:
            if not task.done():
                task.cancel()
        if self.exchange:
            await self.exchange.close()
