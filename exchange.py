"""
exchange.py - 多交易所管理器（修复持仓数据解析，返回完整标准化余额）
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
                    self.exchange.set_sandbox_mode(True)
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

    # ---------- 余额（返回完整标准化数据，持仓可用） ----------
    async def fetch_balance(self):
        """
        获取余额，返回 CCXT 标准格式的完整字典。
        兼容 OKX 模拟盘的特殊数据结构。
        """
        if not self.exchange:
            return {'USDT': {'free': 0}}

        try:
            raw = await self.exchange.fetch_balance()

            # 如果 raw 已经是标准格式，直接返回
            if isinstance(raw, dict) and 'USDT' in raw and isinstance(raw['USDT'], (dict, int, float)):
                # 同时确保其他币种也是 dict 格式，如果不是则转换
                result = {}
                for key, val in raw.items():
                    if isinstance(val, dict):
                        result[key] = val
                    elif isinstance(val, (int, float)):
                        result[key] = {'free': float(val), 'used': 0, 'total': float(val)}
                return result

            # 否则尝试从 info.data 中提取
            info = raw.get('info') if isinstance(raw, dict) else None
            if isinstance(info, dict):
                data_list = info.get('data')
                if isinstance(data_list, list):
                    result = {}
                    for item in data_list:
                        if isinstance(item, dict):
                            ccy = item.get('ccy', '')
                            avail = float(item.get('availBal', 0))
                            frozen = float(item.get('frozenBal', 0))
                            result[ccy] = {
                                'free': avail,
                                'used': frozen,
                                'total': avail + frozen
                            }
                    if result:
                        return result

        except Exception as e:
            logger.error(f"fetch_balance 失败: {e}", exc_info=True)

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