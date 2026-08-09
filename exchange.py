"""
exchange.py - 多交易所管理器（绕过 CCXT 余额，直接请求 OKX 模拟盘）
"""
import os
import random
import asyncio
import aiohttp
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

    # ---------- 行情（使用 CCXT，工作正常） ----------
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

    # ---------- 余额（完全绕过 CCXT，直接请求 OKX 模拟盘） ----------
    async def fetch_balance(self):
        """
        获取 USDT 余额：不依赖 CCXT 的 fetch_balance，
        直接使用 aiohttp 请求 OKX 模拟盘的 /api/v5/account/balance 接口。
        """
        if not self.exchange:
            return {'USDT': {'free': 0}}

        try:
            # 准备 OKX 签名所需的参数
            timestamp = str(int(asyncio.get_event_loop().time() * 1000))
            method = 'GET'
            request_path = '/api/v5/account/balance'
            body = ''

            # 签名字符串
            sign_str = timestamp + method + request_path + body
            import hmac
            import hashlib
            import base64

            signature = base64.b64encode(
                hmac.new(
                    settings.OKX_SECRET_KEY.encode('utf-8'),
                    sign_str.encode('utf-8'),
                    hashlib.sha256
                ).digest()
            ).decode('utf-8')

            headers = {
                'OK-ACCESS-KEY': settings.OKX_API_KEY,
                'OK-ACCESS-SIGN': signature,
                'OK-ACCESS-TIMESTAMP': timestamp,
                'OK-ACCESS-PASSPHRASE': settings.OKX_PASSPHRASE,
                'Content-Type': 'application/json',
            }

            url = 'https://aws.okx.com' + request_path

            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    data = await resp.json()
                    logger.info(f"OKX 余额接口返回: {str(data)[:2000]}")

                    if isinstance(data, dict) and 'data' in data:
                        items = data['data']
                        if isinstance(items, list):
                            for item in items:
                                if isinstance(item, dict) and item.get('ccy') == 'USDT':
                                    avail = item.get('availBal', item.get('cashBal', 0))
                                    return {'USDT': {'free': float(avail)}}

        except Exception as e:
            logger.error(f"直接请求 OKX 余额接口失败: {e}", exc_info=True)

        logger.error("所有余额获取方法均失败")
        return {'USDT': {'free': 0}}

    # ---------- 交易（使用 CCXT，工作正常） ----------
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