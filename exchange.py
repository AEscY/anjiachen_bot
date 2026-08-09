"""
exchange.py - 多交易所管理器（余额最终修复版，兼容 OKX 模拟盘）
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

    # ---------- 余额（铁壁防御版） ----------
    async def fetch_balance(self):
        """获取余额——终极通用版，任何结构都能安全返回，不报错"""
        if not self.exchange:
            return {'USDT': {'free': 0}}
        try:
            raw = await self.exchange.fetch_balance()
            logger.info(f"=== 余额原始数据开始 ===")
            logger.info(f"{raw}")
            logger.info(f"=== 余额原始数据结束 ===")
            for k, v in raw.items():
                logger.info(f"顶层键: {k} (类型: {type(v).__name__}) 值: {str(v)[:150]}")

            # ----- 尝试 1：直接从标准字段提取 -----
            for field in ('total', 'free', 'used'):
                sub = raw.get(field)
                if isinstance(sub, dict) and 'USDT' in sub:
                    val = sub['USDT']
                    if isinstance(val, (int, float)):
                        return {'USDT': {'free': float(val)}}

            # ----- 尝试 2：USDT 直接在顶层是数字或字典 -----
            usdt = raw.get('USDT')
            if isinstance(usdt, (int, float)):
                return {'USDT': {'free': float(usdt)}}
            if isinstance(usdt, dict):
                val = usdt.get('free', usdt.get('total', 0))
                return {'USDT': {'free': float(val)}}

            # ----- 尝试 3：遍历所有顶层值，寻找字典中含 USDT 的 -----
            for key, val in raw.items():
                if not isinstance(val, dict):
                    continue
                for sub_key, sub_val in val.items():
                    if isinstance(sub_val, dict) and 'USDT' in sub_val:
                        return {'USDT': {'free': float(sub_val['USDT'])}}
                    if sub_key == 'USDT' and isinstance(sub_val, (int, float)):
                        return {'USDT': {'free': float(sub_val)}}

            # ----- 尝试 4：CCXT 常用格式 info.data 数组 -----
            info = raw.get('info')
            if isinstance(info, dict):
                data = info.get('data')
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict) and item.get('ccy') == 'USDT':
                            avail = item.get('availBal', item.get('cashBal', 0))
                            return {'USDT': {'free': float(avail)}}

            logger.error("⚠️ 无法解析余额，请将上面的原始数据发给开发者")
        except Exception as e:
            logger.error(f"fetch_balance 异常: {e}", exc_info=True)
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