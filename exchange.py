"""
exchange.py - 多交易所管理器（余额调试版，安全打印原始数据）
"""
import os
import random
import asyncio
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

    # ---------- 行情 ----------
    async def fetch_ticker(self, symbol):
        if self.exchange:
            try:
                return await self.exchange.fetch_ticker(symbol)
            except Exception:
                pass
        return {'last': random.uniform(3000, 3200)}

    async def fetch_ohlcv(self, symbol, timeframe='15m', limit=100):
        if not self.exchange:
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
            try:
                return await self.exchange.fetch_order_book(symbol, limit)
            except Exception:
                pass
        ticker = await self.fetch_ticker(symbol)
        p = ticker['last']
        return {'bids': [[p * 0.9998, 12.5]], 'asks': [[p * 1.0002, 10.2]]}

    async def fetch_funding_rate(self, symbol):
        if self.exchange:
            try:
                res = await self.exchange.fetch_funding_rate(symbol)
                return res.get('fundingRate', 0) if isinstance(res, dict) else 0
            except Exception:
                pass
        return 0

    async def fetch_long_short_ratio(self, symbol):
        if self.exchange:
            try:
                return await self.exchange.fetch_long_short_ratio(symbol)
            except Exception:
                pass
        return 1.0

    # ---------- 余额（安全打印原始数据版） ----------
    async def fetch_balance(self):
        """获取余额，安全打印 CCXT 原始返回数据"""
        if not self.exchange:
            return {'USDT': {'free': 0}}

        try:
            # 尝试 CCXT 标准方法
            raw = await self.exchange.fetch_balance()
            logger.info(f"CCXT fetch_balance 原始数据: {str(raw)[:2000]}")

            # 打印顶层键和类型
            if isinstance(raw, dict):
                for k in list(raw.keys())[:10]:
                    v = raw.get(k)
                    if v is not None:
                        logger.info(f"  键: {k}, 类型: {type(v).__name__}")

            # 尝试解析标准格式
            if isinstance(raw, dict):
                # 直接取 USDT
                usdt = raw.get('USDT')
                if isinstance(usdt, dict):
                    free = usdt.get('free', usdt.get('total', 0))
                    return {'USDT': {'free': float(free)}}
                if isinstance(usdt, (int, float)):
                    return {'USDT': {'free': float(usdt)}}
                # 取 total
                total = raw.get('total')
                if isinstance(total, dict) and 'USDT' in total:
                    return {'USDT': {'free': float(total['USDT'])}}
                # 取 free
                free = raw.get('free')
                if isinstance(free, dict) and 'USDT' in free:
                    return {'USDT': {'free': float(free['USDT'])}}

        except Exception as e:
            logger.error(f"CCXT fetch_balance 失败: {e}")

        try:
            # 直接请求 OKX 私有接口
            logger.info("尝试直接请求 OKX 私有接口...")
            response = await self.exchange.privateGetAccountBalance()
            logger.info(f"OKX 私有接口返回: {str(response)[:2000]}")
            if isinstance(response, dict) and 'data' in response:
                data = response['data']
                if isinstance(data, list) and len(data) > 0:
                    for item in data:
                        if isinstance(item, dict) and item.get('ccy') == 'USDT':
                            avail = item.get('availBal', item.get('cashBal', 0))
                            return {'USDT': {'free': float(avail)}}
        except Exception as e:
            logger.error(f"OKX 私有接口失败: {e}")

        logger.error("所有余额获取方法均失败，请将上面的日志发给开发者")
        return {'USDT': {'free': 0}}

    # ---------- 交易 ----------
    async def create_market_buy_order(self, symbol, amount):
        if self.exchange:
            try:
                return await self.exchange.create_order(symbol, 'market', 'buy', amount)
            except Exception:
                pass
        return None

    async def create_market_sell_order(self, symbol, amount):
        if self.exchange:
            try:
                return await self.exchange.create_order(symbol, 'market', 'sell', amount)
            except Exception:
                pass
        return None

    async def cancel_all_orders(self, symbol):
        if self.exchange:
            try:
                return await self.exchange.cancel_all_orders(symbol)
            except Exception:
                pass
        return True

    async def close(self):
        if self.exchange:
            await self.exchange.close()