"""
exchange.py - 多交易所管理器（修复模拟盘地址 + 安全余额提取）
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
                    # ----- 修正：OKX 模拟盘的正确地址 -----
                    self.exchange.urls['api'] = 'https://demo.okx.com'
                    logger.info("🧪 OKX 模拟盘模式已启用 (demo.okx.com)")
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

    # ---------- 余额（安全提取版） ----------
    async def fetch_balance(self):
        """获取余额，使用最安全的方式从 CCXT 原始数据中提取 USDT"""
        if not self.exchange:
            return {'USDT': {'free': 0}}

        try:
            raw = await self.exchange.fetch_balance()
            logger.info(f"CCXT 余额原始数据: {raw}")

            # 只处理 raw 是字典的情况
            if isinstance(raw, dict):
                # 方法1：直接取 USDT 键
                usdt = raw.get('USDT')
                if isinstance(usdt, dict):
                    free = usdt.get('free', usdt.get('total', 0))
                    return {'USDT': {'free': float(free)}}
                if isinstance(usdt, (int, float)):
                    return {'USDT': {'free': float(usdt)}}

                # 方法2：遍历所有顶层值，只在值是字典时深入查找
                for key, val in raw.items():
                    if not isinstance(val, dict):
                        continue
                    # 在子字典中查找 USDT
                    if 'USDT' in val and isinstance(val['USDT'], (int, float)):
                        return {'USDT': {'free': float(val['USDT'])}}
                    # 查找 free 和 total
                    for field in ('free', 'total'):
                        sub = val.get(field)
                        if isinstance(sub, dict) and 'USDT' in sub and isinstance(sub['USDT'], (int, float)):
                            return {'USDT': {'free': float(sub['USDT'])}}

                # 方法3：查找 info 字段
                info = raw.get('info')
                if isinstance(info, dict):
                    data_list = info.get('data')
                    if isinstance(data_list, list):
                        for item in data_list:
                            if isinstance(item, dict) and item.get('ccy') == 'USDT':
                                avail = item.get('availBal', item.get('cashBal', 0))
                                return {'USDT': {'free': float(avail)}}

            logger.error("无法从 CCXT 余额数据中提取 USDT")
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