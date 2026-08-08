"""
exchange.py - 多交易所管理器 (通过 EXCHANGE_NAME 切换)
"""
import os, random, ccxt.async_support as ccxt
from config import settings, logger

class ExchangeManager:
    def __init__(self):
        self.exchange = None
        self._init_exchange()

    def _get_credentials(self):
        name = settings.EXCHANGE_NAME
        if name == 'okx':
            key = settings.OKX_API_KEY or settings.API_KEY
            secret = settings.OKX_SECRET_KEY or settings.SECRET_KEY
            password = settings.OKX_PASSPHRASE or settings.PASSWORD
        elif name == 'binance':
            key = os.getenv('BINANCE_API_KEY', '') or settings.API_KEY
            secret = os.getenv('BINANCE_SECRET_KEY', '') or settings.SECRET_KEY
            password = settings.PASSWORD
        else:
            key = settings.API_KEY
            secret = settings.SECRET_KEY
            password = settings.PASSWORD
        return key, secret, password

    def _init_exchange(self):
        name = settings.EXCHANGE_NAME
        key, secret, password = self._get_credentials()

        if not key:
            logger.warning(f"⚠️ 未配置 {name} API 密钥，将使用模拟数据")
            return

        try:
            exchange_class = getattr(ccxt, name, None)
            if exchange_class is None:
                logger.error(f"❌ 不支持的交易所: {name}")
                return

            config = {
                'apiKey': key,
                'secret': secret,
                'enableRateLimit': True,
            }
            if name == 'okx':
                config['password'] = password
            elif name == 'binance':
                pass
            elif name == 'bybit':
                config['password'] = password

            self.exchange = exchange_class(config)

            if settings.IS_SANDBOX:
                if name == 'okx':
                    self.exchange.set_sandbox_mode(True)
                elif name == 'binance':
                    self.exchange.urls['api'] = self.exchange.urls['test']

            logger.info(f"✅ 交易所连接成功: {name}")
        except Exception as e:
            logger.warning(f"交易所连接失败 ({name}): {e}")

    async def fetch_ticker(self, symbol):
        if self.exchange:
            try: return await self.exchange.fetch_ticker(symbol)
            except: pass
        return {'last': random.uniform(3000, 3200)}

    async def fetch_ohlcv(self, symbol, timeframe='15m', limit=100):
        if self.exchange:
            try: return await self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
            except: pass
        return []

    async def fetch_orderbook(self, symbol, limit=5):
        """获取真实盘口数据，失败时降级为模拟数据"""
        if self.exchange:
            try: return await self.exchange.fetch_order_book(symbol, limit)
            except: pass
        # 降级模拟数据
        ticker = await self.fetch_ticker(symbol)
        p = ticker['last']
        return {
            'bids': [[p * 0.9998, 12.5], [p * 0.9995, 25.0]],
            'asks': [[p * 1.0002, 10.2], [p * 1.0005, 30.1]]
        }

    async def fetch_funding_rate(self, symbol):
        if self.exchange:
            try:
                res = await self.exchange.fetch_funding_rate(symbol)
                return res.get('fundingRate', 0) if isinstance(res, dict) else 0
            except: pass
        return 0

    async def fetch_balance(self):
        if self.exchange:
            try: return await self.exchange.fetch_balance()
            except: pass
        return {'USDT': {'free': 0}}

    async def create_market_sell_order(self, symbol, amount):
        if self.exchange and amount > 0:
            try: return await self.exchange.create_order(symbol, 'market', 'sell', amount)
            except: pass
        return None

    async def cancel_all_orders(self, symbol):
        if self.exchange:
            try: return await self.exchange.cancel_all_orders(symbol)
            except: pass
        return True

    async def close(self):
        if self.exchange:
            await self.exchange.close()
