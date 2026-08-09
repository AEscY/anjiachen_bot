"""
exchange.py - 多交易所管理器（余额解析已修复，兼容 OKX 模拟盘）
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
        """初始化交易所连接"""
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

            # 模拟盘配置
            if settings.IS_SANDBOX:
                if name == 'okx':
                    self.exchange.urls['api'] = 'https://aws.okx.com'
                    logger.info("🧪 OKX 模拟盘模式已启用")
                elif name == 'binance':
                    self.exchange.urls['api'] = self.exchange.urls['test']

            logger.info(f"✅ 交易所连接成功: {name}")
        except Exception as e:
            logger.warning(f"交易所连接失败 ({name}): {e}")

    # ---------- 行情相关 ----------
    async def fetch_ticker(self, symbol):
        if self.exchange:
            try:
                return await self.exchange.fetch_ticker(symbol)
            except Exception:
                pass
        return {'last': random.uniform(3000, 3200)}

    async def fetch_ohlcv(self, symbol, timeframe='15m', limit=100):
        """获取 K 线数据，带重试"""
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
        """获取盘口数据"""
        if self.exchange:
            try:
                return await self.exchange.fetch_order_book(symbol, limit)
            except Exception:
                pass
        ticker = await self.fetch_ticker(symbol)
        p = ticker['last']
        return {'bids': [[p * 0.9998, 12.5]], 'asks': [[p * 1.0002, 10.2]]}

    async def fetch_funding_rate(self, symbol):
        """获取资金费率"""
        if self.exchange:
            try:
                res = await self.exchange.fetch_funding_rate(symbol)
                return res.get('fundingRate', 0) if isinstance(res, dict) else 0
            except Exception:
                pass
        return 0

    async def fetch_long_short_ratio(self, symbol):
        """获取多空持仓比"""
        if self.exchange:
            try:
                return await self.exchange.fetch_long_short_ratio(symbol)
            except Exception:
                pass
        return 1.0

    async def fetch_balance(self):
        """
        获取余额（已彻底修复，兼容 OKX 模拟盘的特殊结构）
        原理：CCXT 返回的数据可能是嵌套的，我们优先从最外层查找 USDT，
        如果失败则尝试 info 字段下的子结构。
        """
        if not self.exchange:
            return {'USDT': {'free': 0}}
        try:
            raw = await self.exchange.fetch_balance()
            logger.info(f"余额原始数据: {raw}")

            # 1. 尝试从顶层直接获取 USDT
            usdt = raw.get('USDT')
            if isinstance(usdt, dict):
                free = usdt.get('free', usdt.get('total', 0))
                return {'USDT': {'free': float(free)}}
            if isinstance(usdt, (int, float)):
                return {'USDT': {'free': float(usdt)}}

            # 2. 尝试从 'total' 字段获取
            total = raw.get('total')
            if isinstance(total, dict) and 'USDT' in total:
                return {'USDT': {'free': float(total['USDT'])}}

            # 3. 尝试从 'free' 字段获取
            free = raw.get('free')
            if isinstance(free, dict) and 'USDT' in free:
                return {'USDT': {'free': float(free['USDT'])}}

            # 4. 尝试从 'info' 字段获取（OKX 模拟盘有时将余额放在 info 里）
            info = raw.get('info')
            if isinstance(info, dict):
                # 有时 info 里面有 'data' 数组
                data = info.get('data')
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict) and item.get('ccy') == 'USDT':
                            avail = item.get('availBal', item.get('cashBal', 0))
                            return {'USDT': {'free': float(avail)}}
                # 有时直接是字典
                if 'USDT' in info and isinstance(info['USDT'], (int, float)):
                    return {'USDT': {'free': float(info['USDT'])}}
                # 再尝试遍历 info 内部
                for sub_key, sub_val in info.items():
                    if isinstance(sub_val, dict) and 'USDT' in sub_val:
                        return {'USDT': {'free': float(sub_val['USDT'])}}

            # 5. 遍历所有顶层键，只处理值为字典的
            for key, val in raw.items():
                if not isinstance(val, dict):
                    continue
                if 'USDT' in str(key).upper():
                    free_val = val.get('free', val.get('total', 0))
                    return {'USDT': {'free': float(free_val)}}
                # 递归查找内部
                for sub_key, sub_val in val.items():
                    if isinstance(sub_val, dict) and 'USDT' in sub_val:
                        return {'USDT': {'free': float(sub_val['USDT'])}}

            logger.error(f"无法解析余额结构，请将上面的原始数据发送给开发者")
        except Exception as e:
            logger.error(f"获取余额失败: {e}")
        return {'USDT': {'free': 0}}

    # ---------- 交易相关 ----------
    async def create_market_buy_order(self, symbol, amount):
        """市价买入"""
        if self.exchange:
            try:
                return await self.exchange.create_order(symbol, 'market', 'buy', amount)
            except Exception:
                pass
        return None

    async def create_market_sell_order(self, symbol, amount):
        """市价卖出"""
        if self.exchange:
            try:
                return await self.exchange.create_order(symbol, 'market', 'sell', amount)
            except Exception:
                pass
        return None

    async def cancel_all_orders(self, symbol):
        """撤销所有挂单"""
        if self.exchange:
            try:
                return await self.exchange.cancel_all_orders(symbol)
            except Exception:
                pass
        return True

    async def close(self):
        if self.exchange:
            await self.exchange.close()