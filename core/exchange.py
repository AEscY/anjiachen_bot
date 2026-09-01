"""
exchange.py - 多交易所管理器（优化：连接池 + 统一重试装饰器）
"""
import os
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
            logger.warning(f"⚠️ 未配置 {name} API 密钥")
            return
        try:
            if not settings.IS_SANDBOX and not settings.LIVE_TRADING_CONFIRM:
                logger.error('⛔ 实盘被阻止：请设置 LIVE_TRADING_CONFIRM=true')
                return
            exchange_class = getattr(ccxt, name, None)
            if exchange_class is None:
                logger.error(f"❌ 不支持的交易所: {name}")
                return
            config = {'apiKey': key, 'secret': secret, 'enableRateLimit': True}
            if name in ('okx', 'bybit'):
                config['password'] = password
            self.exchange = exchange_class(config)

            if settings.IS_SANDBOX:
                if name in ('okx', 'binance'):
                    self.exchange.set_sandbox_mode(True)
                    logger.info(f"🧪 {name.upper()} 模拟盘模式已启用")
            logger.info(f"✅ 交易所连接成功: {name}")
        except Exception as e:
            logger.warning(f"交易所连接失败 ({name}): {e}")

    async def _retry_call(self, func, *args, max_retries=3, **kwargs):
        for attempt in range(max_retries):
            try:
                return await func(*args, **kwargs)
            except Exception as e:
                err_msg = str(e)
                if '50011' in err_msg or 'Too Many Requests' in err_msg:
                    wait = 2 ** (attempt + 3)
                else:
                    wait = 2 ** attempt
                if attempt == max_retries - 1:
                    logger.error(f"重试 {max_retries} 次后仍失败: {e}")
                    raise
                logger.warning(f"调用失败 (第{attempt+1}次): {e}，等待 {wait}s 重试")
                await asyncio.sleep(wait)

    async def fetch_ticker(self, symbol):
        if not self.exchange:
            return None
        try:
            return await self._retry_call(self.exchange.fetch_ticker, symbol, max_retries=2)
        except Exception:
            return None

    async def fetch_ohlcv(self, symbol, timeframe='15m', limit=100):
        if not self.exchange:
            return None
        try:
            return await self._retry_call(self.exchange.fetch_ohlcv, symbol, timeframe, limit=limit)
        except Exception:
            return None

    async def fetch_orderbook(self, symbol, limit=5):
        if not self.exchange:
            return None
        try:
            return await self._retry_call(self.exchange.fetch_order_book, symbol, limit, max_retries=2)
        except Exception:
            return None

    # 注：fetch_funding_rate 已删除 —— 资金费率是永续合约概念，
    # 本项目为现货网格，该方法在全项目中 0 调用，且写库时恒为 0。

    async def fetch_balance(self):
        if not self.exchange:
            return {'USDT': {'free': 0}}
        try:
            raw = await self._retry_call(self.exchange.fetch_balance, max_retries=2)
            result = {}
            system_keys = {'info', 'free', 'used', 'total', 'datetime', 'timestamp'}
            for key, val in raw.items():
                if key in system_keys:
                    continue
                if isinstance(val, dict) and 'free' in val:
                    result[key] = {'free': float(val.get('free', 0)), 'used': float(val.get('used', 0)), 'total': float(val.get('total', 0))}
                elif isinstance(val, (int, float)):
                    result[key] = {'free': float(val), 'used': 0, 'total': float(val)}
            return result
        except Exception:
            return {'USDT': {'free': 0}}

    async def _prepare_amount(self, symbol, amount):
        if not self.exchange or amount <= 0:
            return 0.0
        try:
            if not self.exchange.markets:
                await self.exchange.load_markets()
            amount = float(self.exchange.amount_to_precision(symbol, amount))
            market = self.exchange.market(symbol)
            min_amt = ((market.get('limits') or {}).get('amount') or {}).get('min')
            max_amt = ((market.get('limits') or {}).get('amount') or {}).get('max')
            if min_amt and amount < float(min_amt):
                return 0.0
            if max_amt and amount > float(max_amt):
                amount = float(max_amt)
            return amount
        except Exception:
            return 0.0

    async def round_price(self, symbol, price) -> float:
        """按交易所价格精度取整"""
        if not self.exchange or price <= 0:
            return 0.0
        try:
            if not self.exchange.markets:
                await self.exchange.load_markets()
            return float(self.exchange.price_to_precision(symbol, float(price)))
        except Exception:
            return float(price)

    async def round_amount(self, symbol, amount) -> float:
        """按交易所数量精度取整，并夹在最小/最大下单量之间"""
        return await self._prepare_amount(symbol, amount)

    async def create_limit_order(self, symbol, side, amount, price,
                                 client_id: str = ""):
        """
        限价挂单（网格核心）。
        client_id 作为幂等键透传给交易所：同一 id 重复提交不会产生第二张单，
        这是「崩溃重启后不重复下单」的关键。
        """
        if not self.exchange:
            return None
        try:
            amount = await self._prepare_amount(symbol, amount)
            if amount <= 0:
                return None
            price = await self.round_price(symbol, price)
            if price <= 0:
                return None

            params = {}
            if client_id:
                # ccxt 统一用 clientOrderId；OKX 底层字段为 clOrdId，ccxt 会自动映射
                params['clientOrderId'] = client_id

            order = await self._retry_call(
                self.exchange.create_order, symbol, 'limit', side, amount, price,
                params, max_retries=2)
            if order and client_id:
                order['clientOrderId'] = client_id
            return order
        except Exception as e:
            logger.error(f"限价单失败 {symbol} {side}: {e}")
            return None

    async def fetch_open_orders(self, symbol):
        """查询未成交挂单 —— 网格对账与成交检测的基础"""
        if not self.exchange:
            return []
        try:
            return await self._retry_call(
                self.exchange.fetch_open_orders, symbol, max_retries=2) or []
        except Exception as e:
            logger.warning(f"查询未成交单失败 {symbol}: {e}")
            return []

    async def fetch_order(self, order_id, symbol):
        """查询单张订单最终状态"""
        if not self.exchange:
            return None
        try:
            o = await self._retry_call(
                self.exchange.fetch_order, order_id, symbol, max_retries=2)
            if not o:
                return None
            fee = o.get('fee') or {}
            o['_fee_cost'] = float(fee.get('cost') or 0)
            o['_fee_currency'] = fee.get('currency') or ''
            return o
        except Exception as e:
            logger.debug(f"查单失败 {order_id}: {e}")
            return None

    async def cancel_order(self, order_id, symbol) -> bool:
        """撤销单张订单"""
        if not self.exchange:
            return False
        try:
            await self._retry_call(
                self.exchange.cancel_order, order_id, symbol, max_retries=2)
            return True
        except Exception as e:
            logger.debug(f"撤单失败 {order_id}: {e}")
            return False

    async def _finalize_order(self, order, symbol):
        if not order:
            return None
        try:
            oid = order.get('id')
            if oid:
                for _ in range(6):
                    if order.get('status') == 'closed' and float(order.get('filled') or 0) > 0:
                        break
                    try:
                        fresh = await self.exchange.fetch_order(oid, symbol)
                        if fresh:
                            order.update(fresh)
                    except Exception:
                        pass
                    await asyncio.sleep(0.5)
            filled = float(order.get('filled') or 0)
            if filled <= 0 or order.get('status') == 'canceled':
                return None
            order['filled'] = filled
            order['average'] = float(order.get('average') or order.get('price') or 0)
            fee = order.get('fee') or {}
            order['_fee_cost'] = float(fee.get('cost') or 0)
            order['_fee_currency'] = fee.get('currency') or ''
            return order
        except Exception:
            return None

    async def create_market_buy_order(self, symbol, amount):
        if not self.exchange:
            return None
        try:
            amount = await self._prepare_amount(symbol, amount)
            if amount <= 0:
                return None
            order = await self._retry_call(self.exchange.create_order, symbol, 'market', 'buy', amount)
            return await self._finalize_order(order, symbol)
        except Exception as e:
            logger.error(f"市价买单失败 {symbol}: {e}")
            return None

    async def create_market_sell_order(self, symbol, amount):
        if not self.exchange:
            return None
        try:
            amount = await self._prepare_amount(symbol, amount)
            if amount <= 0:
                return None
            order = await self._retry_call(self.exchange.create_order, symbol, 'market', 'sell', amount)
            return await self._finalize_order(order, symbol)
        except Exception as e:
            logger.error(f"市价卖单失败 {symbol}: {e}")
            return None

    async def cancel_all_orders(self, symbol):
        if not self.exchange:
            return False
        try:
            await self.exchange.cancel_all_orders(symbol)
            return True
        except Exception:
            return False

    async def close(self):
        if self.exchange:
            await self.exchange.close()
            logger.info("🔌 交易所连接已关闭")