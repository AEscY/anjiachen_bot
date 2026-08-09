"""
exchange.py - 多交易所管理器（兼容余额结构）
"""
import os, random, asyncio
import ccxt.async_support as ccxt
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
            config = {'apiKey': key, 'secret': secret, 'enableRateLimit': True}
            if name in ('okx', 'bybit'):
                config['password'] = password
            self.exchange = exchange_class(config)

            if settings.IS_SANDBOX:
                if name == 'okx':
                    self.exchange.urls['api'] = 'https://aws.okx.com'
                    logger.info("🧪 OKX 模拟盘模式已启用")
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
        if not self.exchange:
            logger.warning("交易所未连接，无法获取K线")
            return []
        for attempt in range(3):
            try:
                data = await self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
                if data and len(data) > 0:
                    return data
            except Exception as e:
                logger.warning(f"K线获取失败 (第{attempt+1}次): {e}")
            await asyncio.sleep(2)
        return []

    async def fetch_orderbook(self, symbol, limit=5):
        if self.exchange:
            try: return await self.exchange.fetch_order_book(symbol, limit)
            except: pass
        ticker = await self.fetch_ticker(symbol)
        p = ticker['last']
        return {'bids': [[p * 0.9998, 12.5]], 'asks': [[p * 1.0002, 10.2]]}

    async def fetch_funding_rate(self, symbol):
        if self.exchange:
            try:
                res = await self.exchange.fetch_funding_rate(symbol)
                return res.get('fundingRate', 0) if isinstance(res, dict) else 0
            except: pass
        return 0

    async def fetch_long_short_ratio(self, symbol):
        if self.exchange:
            try: return await self.exchange.fetch_long_short_ratio(symbol)
            except: pass
        return 1.0

    async def fetch_balance(self):
        """获取余额，兼容多种数据结构，修复字符串索引错误"""
        if not self.exchange:
            return {'USDT': {'free': 0}}
        try:
            raw = await self.exchange.fetch_balance()
            logger.info(f"余额原始数据: {raw}")

            # 1. 标准 CCXT 格式：{'USDT': {'free': xxx, 'used': xxx, 'total': xxx}}
            if 'USDT' in raw and isinstance(raw['USDT'], dict):
                usdt_data = raw['USDT']
                free = usdt_data.get('free', usdt_data.get('total', 0))
                return {'USDT': {'free': float(free)}}

            # 2. 自由格式：{'free': {'USDT': xxx}}
            if 'free' in raw and isinstance(raw['free'], dict) and 'USDT' in raw['free']:
                return {'USDT': {'free': float(raw['free']['USDT'])}}

            # 3. 总量格式：{'total': {'USDT': xxx}}
            if 'total' in raw and isinstance(raw['total'], dict) and 'USDT' in raw['total']:
                return {'USDT': {'free': float(raw['total']['USDT'])}}

            # 4. 直接数值：{'USDT': 5000}
            if 'USDT' in raw and isinstance(raw['USDT'], (int, float)):
                return {'USDT': {'free': float(raw['USDT'])}}

            # 5. 遍历查找（仅当值为字典或数值时处理）
            for key, val in raw.items():
                if 'USDT' in str(key).upper():
                    if isinstance(val, dict):
                        free_val = val.get('free', val.get('total', 0))
                        return {'USDT': {'free': float(free_val)}}
                    elif isinstance(val, (int, float)):
                        return {'USDT': {'free': float(val)}}

            logger.warning(f"无法解析余额结构: {raw}")
        except Exception as e:
            logger.error(f"获取余额失败: {e}")
        return {'USDT': {'free': 0}}

    async def create_market_buy_order(self, symbol, amount):
        if self.exchange:
            try: return await self.exchange.create_order(symbol, 'market', 'buy', amount)
            except: pass
        return None

    async def create_market_sell_order(self, symbol, amount):
        if self.exchange:
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