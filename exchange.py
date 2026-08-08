"""
exchange.py - 多交易所管理器
根据 EXCHANGE_NAME 环境变量自动切换交易所，支持 OKX、Binance 等。
"""
import os
import random
import ccxt.async_support as ccxt
from config import settings, logger

class ExchangeManager:
    def __init__(self):
        self.exchange = None
        self._init_exchange()

    def _get_credentials(self):
        """根据交易所名称返回 API 密钥"""
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
            # 部分交易所需要额外字段
            if name == 'okx':
                config['password'] = password
            elif name == 'bybit':
                config['password'] = password

            self.exchange = exchange_class(config)

            # 模拟盘设置
            if settings.IS_SANDBOX:
                if name == 'okx':
                    self.exchange.set_sandbox_mode(True)
                elif name == 'binance':
                    self.exchange.urls['api'] = self.exchange.urls['test']
            logger.info(f"✅ 交易所连接成功: {name}")
        except Exception as e:
            logger.warning(f"交易所连接失败 ({name}): {e}")

    # ---------- 行情相关 ----------
    async def fetch_ticker(self, symbol):
        if self.exchange:
            try: return await self.exchange.fetch_ticker(symbol)
            except: pass
        # 降级为模拟数据
        return {'last': random.uniform(3000, 3200)}

    async def fetch_ohlcv(self, symbol, timeframe='15m', limit=100):
        if self.exchange:
            try: return await self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
            except: pass
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
        """获取多空持仓比，部分交易所可能不支持"""
        if self.exchange:
            try: return await self.exchange.fetch_long_short_ratio(symbol)
            except: pass
        return 1.0

    # ---------- 账户与交易 ----------
    async def fetch_balance(self):
        if self.exchange:
            try: return await self.exchange.fetch_balance()
            except: pass
        return {'USDT': {'free': 0}}

    async def create_market_buy_order(self, symbol, amount):
        """市价买入，amount 为基础货币数量"""
        if self.exchange:
            try: return await self.exchange.create_order(symbol, 'market', 'buy', amount)
            except: pass
        return None

    async def create_market_sell_order(self, symbol, amount):
        """市价卖出"""
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
