"""
exchange.py - 多交易所管理器（修复余额解析、订单取消返回值）
增加：指数退避重试，处理 OKX 限频错误
"""
import os
import asyncio
import traceback
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
            logger.warning(f"⚠️ 未配置 {name} API 密钥，所有数据将不可用")
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
                    self.exchange.set_sandbox_mode(True)
                    logger.info("🧪 Binance 模拟盘模式已启用")
                else:
                    logger.warning(f"⚠️ 交易所 {name} 暂不支持模拟盘模式")

            logger.info(f"✅ 交易所连接成功: {name}")
        except Exception as e:
            logger.warning(f"交易所连接失败 ({name}): {e}")

    async def fetch_ticker(self, symbol):
        if self.exchange:
            try:
                ticker = await self.exchange.fetch_ticker(symbol)
                if ticker and 'last' in ticker:
                    return ticker
            except Exception as e:
                logger.warning(f"获取现价失败 {symbol}: {e}")
        return None

    async def fetch_ohlcv(self, symbol, timeframe='15m', limit=100):
        """
        获取K线，增加指数退避重试，针对 OKX 限频错误 (50011) 特殊处理
        """
        if not self.exchange:
            return None
        for attempt in range(3):
            try:
                data = await self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
                if data and len(data) > 0:
                    return data
            except Exception as e:
                err_msg = str(e)
                if '50011' in err_msg or 'Too Many Requests' in err_msg:
                    wait = 2 ** (attempt + 2)  # 4, 8, 16 秒
                else:
                    wait = 2 ** attempt  # 1, 2, 4 秒
                logger.warning(f"K线获取失败 {symbol} (第{attempt+1}次): {e}，等待 {wait}s 重试")
                await asyncio.sleep(wait)
        logger.error(f"K线获取彻底失败 {symbol}")
        return None

    async def fetch_orderbook(self, symbol, limit=5):
        if self.exchange:
            try:
                return await self.exchange.fetch_order_book(symbol, limit)
            except Exception as e:
                logger.warning(f"获取盘口失败 {symbol}: {e}")
        return None

    async def fetch_funding_rate(self, symbol):
        if not self.exchange:
            return None
        try:
            if settings.EXCHANGE_NAME == 'okx' and '/' in symbol and ':' not in symbol:
                return None
            res = await self.exchange.fetch_funding_rate(symbol)
            return res.get('fundingRate', None) if isinstance(res, dict) else None
        except Exception:
            return None

    async def fetch_long_short_ratio(self, symbol):
        if self.exchange:
            try:
                return await self.exchange.fetch_long_short_ratio(symbol)
            except Exception as e:
                logger.warning(f"获取多空比失败 {symbol}: {e}")
        return None

    async def fetch_balance(self):
        if not self.exchange:
            return {'USDT': {'free': 0}}
        try:
            raw = await self.exchange.fetch_balance()
            if not raw:
                return {'USDT': {'free': 0}}
            result = {}
            system_keys = {'info', 'free', 'used', 'total', 'datetime', 'timestamp'}
            for key, val in raw.items():
                if key in system_keys:
                    continue
                if isinstance(val, dict) and 'free' in val:
                    result[key] = {'free': float(val.get('free', 0)), 'used': float(val.get('used', 0)), 'total': float(val.get('total', 0))}
                elif isinstance(val, (int, float)):
                    result[key] = {'free': float(val), 'used': 0, 'total': float(val)}
            if not result:
                logger.warning("⚠️ 余额解析结果为空，请检查交易所返回格式")
                return {'USDT': {'free': 0}}
            return result
        except Exception as e:
            logger.error(f"获取余额异常: {e}\n{traceback.format_exc()}")
            return {'USDT': {'free': 0}}

    async def create_market_buy_order(self, symbol, amount):
        if not self.exchange:
            logger.error("❌ 交易所未初始化")
            return None
        try:
            order = await self.exchange.create_order(symbol, 'market', 'buy', amount)
            logger.info(f"✅ 市价买单成功 {symbol} 数量:{amount:.6f}")
            return order
        except Exception as e:
            logger.error(f"❌ 市价买单失败 [{symbol}] amount={amount:.6f}: {e}\n{traceback.format_exc()}")
            return None

    async def create_market_sell_order(self, symbol, amount):
        if not self.exchange:
            logger.error("❌ 交易所未初始化")
            return None
        try:
            order = await self.exchange.create_order(symbol, 'market', 'sell', amount)
            logger.info(f"✅ 市价卖单成功 {symbol} 数量:{amount:.6f}")
            return order
        except Exception as e:
            logger.error(f"❌ 市价卖单失败 [{symbol}] amount={amount:.6f}: {e}\n{traceback.format_exc()}")
            return None

    async def cancel_all_orders(self, symbol):
        if self.exchange:
            try:
                result = await self.exchange.cancel_all_orders(symbol)
                logger.info(f"✅ 已取消 {symbol} 全部挂单")
                return result is not None
            except Exception as e:
                logger.warning(f"取消订单失败 {symbol}: {e}")
                return False
        return False

    async def close(self):
        if self.exchange:
            await self.exchange.close()
            logger.info("🔌 交易所连接已关闭")