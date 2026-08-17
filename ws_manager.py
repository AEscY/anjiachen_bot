"""
ws_manager.py - WebSocket 实时数据管理器（兼容 OKX 单个订阅）
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
        self._orderbook_tasks = []  # 保存订单簿订阅任务

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
        """批量订阅 Ticker（OKX 支持 watch_tickers）"""
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
                                    'percentage': ticker.get('percentage', 0),
                                    'volume': ticker.get('baseVolume', 0),
                                    'timestamp': time.time()
                                }
                self._reconnect_attempt = 0
            except Exception as e:
                logger.warning(f"Ticker WebSocket 断线: {e}")
                self._reconnect_attempt += 1
                wait = min(60, 2 ** self._reconnect_attempt)
                logger.info(f"🔄 等待 {wait}s 后重连 Ticker...")
                await asyncio.sleep(wait)
                await self.connect()
                # 重连后重新订阅
                if self.exchange:
                    try:
                        await self.exchange.watch_tickers(symbols)
                    except:
                        pass

    async def _watch_single_orderbook(self, symbol, limit=5):
        """单个交易对的订单簿订阅（OKX 兼容）"""
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
                self._reconnect_attempt = 0
            except Exception as e:
                logger.warning(f"订单簿 {symbol} WebSocket 断线: {e}")
                self._reconnect_attempt += 1
                wait = min(60, 2 ** self._reconnect_attempt)
                logger.info(f"🔄 订单簿 {symbol} 等待 {wait}s 后重连...")
                await asyncio.sleep(wait)
                await self.connect()
                # 继续循环，重新 watch_order_book

    async def watch_orderbooks(self, symbols, limit=5):
        """
        为每个交易对单独启动一个订阅任务（兼容 OKX）
        注意：此方法会阻塞，但会创建后台任务，建议在外部用 asyncio.create_task 调用
        """
        # 取消旧任务（如果有）
        for task in self._orderbook_tasks:
            if not task.done():
                task.cancel()
        self._orderbook_tasks.clear()

        # 为每个 symbol 创建独立任务
        for symbol in symbols:
            task = asyncio.create_task(self._watch_single_orderbook(symbol, limit))
            self._orderbook_tasks.append(task)

        # 等待所有任务结束（或一直运行）
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