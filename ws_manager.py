"""
ws_manager.py - WebSocket 实时数据管理器
替代 REST 轮询，通过 ccxt.pro 的 watch_* 方法获取实时价格和订单簿。
延迟从 30s 降到 100ms 以内。
"""
import asyncio
import ccxt.pro as ccxt_pro
from config import settings, logger


class WSDataManager:
    def __init__(self, exchange_rest):
        self.rest = exchange_rest
        self.exchange = None
        self.tickers = {}       # symbol -> {last, bid, ask, timestamp}
        self.orderbooks = {}    # symbol -> {bids, asks, timestamp}
        self._running = False

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
            self.exchange.urls['api'] = self.exchange.urls['test']
            self.exchange.set_sandbox_mode(True)

        logger.info(f"🔌 WebSocket 已连接到 {name}")
        return True

    async def watch_tickers(self, symbols):
        """实时监听价格变动"""
        self._running = True
        while self._running:
            try:
                ticker = await self.exchange.watch_ticker(' '.join(symbols))
                if ticker and 'symbol' in ticker:
                    self.tickers[ticker['symbol']] = {
                        'last': ticker.get('last', 0),
                        'bid': ticker.get('bid', 0),
                        'ask': ticker.get('ask', 0),
                        'timestamp': asyncio.get_event_loop().time()
                    }
            except Exception as e:
                logger.warning(f"WebSocket 断线，3 秒后重连: {e}")
                await asyncio.sleep(3)

    async def watch_orderbooks(self, symbols, limit=5):
        """实时监听订单簿"""
        while self._running:
            try:
                ob = await self.exchange.watch_order_book(' '.join(symbols), limit)
                if ob and 'symbol' in ob:
                    self.orderbooks[ob['symbol']] = {
                        'bids': ob.get('bids', []),
                        'asks': ob.get('asks', []),
                        'timestamp': asyncio.get_event_loop().time()
                    }
            except Exception as e:
                logger.warning(f"WebSocket 订单簿断线: {e}")
                await asyncio.sleep(3)

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