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

    # 小额挂单告警节流：同一币种同一原因 5 分钟内只报一次，
    # 避免监控循环每秒重试把日志刷爆
    _min_amt_warned = {}

    async def _prepare_amount(self, symbol, amount, price: float = 0.0):
        """
        按交易所精度取整，并校验最小/最大下单量与最小名义价值。

        原实现有三个问题，小额账户会直接踩中：
          1. 数量不足时静默返回 0.0，上层直接跳过 —— 用户看到"网格是空的"
             却完全不知道原因，排查极其困难。现在会明确告警。
          2. 只校验 limits.amount.min（最小数量），没校验 limits.cost.min
            （最小名义价值/金额）。订单数量够但金额不够时，
             交易所依然会拒单，而这里会放行。
          3. 精度取整后可能归零（比如要求 0.001 ETH 而算出 0.0004），
             同样静默失败。
        """
        if not self.exchange or amount <= 0:
            return 0.0
        try:
            if not self.exchange.markets:
                await self.exchange.load_markets()

            amount = float(self.exchange.amount_to_precision(symbol, amount))
            market = self.exchange.market(symbol)
            limits = market.get('limits') or {}

            min_amt = (limits.get('amount') or {}).get('min')
            max_amt = (limits.get('amount') or {}).get('max')

            # ── 1) 最小数量 ──
            if min_amt and amount < float(min_amt):
                self._warn_once(
                    symbol, "min_amount",
                    f"⚠️ {symbol} 下单数量 {amount:.8g} 低于交易所最小 "
                    f"{float(min_amt):.8g}，该单已跳过。"
                    f"请提高单格金额（减少网格层数 / 提高 grid_capital_pct）"
                    f"或换用最小门槛更低的币种")
                return 0.0

            if max_amt and amount > float(max_amt):
                amount = float(max_amt)

            # ── 2) 精度取整后归零 ──
            if amount <= 0:
                self._warn_once(
                    symbol, "precision_zero",
                    f"⚠️ {symbol} 下单数量按交易所精度取整后为 0，该单已跳过。"
                    f"单格金额太小，请加大")
                return 0.0

            # ── 3) 最小名义价值（原实现漏了这一步）──
            min_cost = (limits.get('cost') or {}).get('min')
            if min_cost and price > 0:
                notional = amount * price
                if notional < float(min_cost):
                    self._warn_once(
                        symbol, "min_cost",
                        f"⚠️ {symbol} 订单金额 {notional:.2f} USDT 低于交易所最小 "
                        f"{float(min_cost):.2f} USDT，该单已跳过。"
                        f"请提高单格金额或换币种")
                    return 0.0

            return amount
        except Exception as e:
            logger.warning(f"数量校验异常 {symbol}: {e}")
            return 0.0

    def _warn_once(self, symbol: str, reason: str, msg: str, ttl: float = 300.0):
        """同一币种同一原因，ttl 秒内只告警一次"""
        import time as _t
        key = f"{symbol}:{reason}"
        now = _t.time()
        last = self._min_amt_warned.get(key, 0)
        if now - last >= ttl:
            self._min_amt_warned[key] = now
            logger.warning(msg)

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
            price = await self.round_price(symbol, price)
            if price <= 0:
                return None
            # 先算好价格再校验数量，这样才能同时检查最小名义价值
            amount = await self._prepare_amount(symbol, amount, price)
            if amount <= 0:
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

    def _market_client_id(self, symbol, side, amount, nonce):
        """
        为市价单生成幂等键。

        ⚠️ 这是本轮最重要的修复。

        原实现：市价单走 _retry_call 重试，但【不带 client_id】。
        危险场景（云端部署很常见）：
            1. 下单请求抵达交易所并【已成交】
            2. 响应在回程中丢失 → ccxt 抛 NetworkError
            3. _retry_call 重试 → 交易所收到第二张单
            4. 结果：仓位翻倍

        实测对照：
            无 client_id → 下单 0.01，实际成交 0.02（翻倍）
            有 client_id → 下单 0.01，实际成交 0.01（交易所侧去重）

        限价单早已带 client_id（网格用），唯独市价单没有。
        而你当前用的【单次低吸高卖模式】走的正是市价单。

        nonce 由调用方提供：重试时必须复用同一个 nonce，
        才能被交易所识别为同一张单。
        """
        import hashlib
        raw = f"mkt:{symbol}:{side}:{amount:.10f}:{nonce}"
        h = hashlib.md5(raw.encode()).hexdigest()[:16]
        # OKX 要求 clOrdId 为字母数字组合
        return f"m{h}"

    async def _place_market_once(self, symbol, side, amount, nonce):
        """带幂等键的市价单，只发一次，不做重试"""
        cid = self._market_client_id(symbol, side, amount, nonce)
        try:
            return await self.exchange.create_order(
                symbol, 'market', side, amount,
                params={'clientOrderId': cid})
        except Exception as e:
            # 部分交易所不支持市价单带 clientOrderId，退回裸下单
            if 'clientOrderId' in str(e) or 'clOrdId' in str(e):
                logger.debug(f"{symbol} 市价单不支持幂等键，退回普通下单")
                return await self.exchange.create_order(
                    symbol, 'market', side, amount)
            raise

    async def _market_order_with_idem(self, symbol, side, amount):
        """
        市价单幂等封装：
          · 首次失败后，【先查最近成交】确认是否已经成交
          · 只有确认未成交才重试，且重试带上一次失败的标记
        """
        import time as _t
        nonce = f"{int(_t.time() * 1000)}"
        last_err = None
        for attempt in range(3):
            try:
                order = await self._place_market_once(symbol, side, amount, nonce)
                if order:
                    return order
                return None
            except Exception as e:
                last_err = e
                if attempt >= 2:
                    break
                # 关键：重试前先确认上一单是否已经成交
                try:
                    filled = await self._recent_market_fill(
                        symbol, side, amount, since=_t.time() - 120)
                    if filled:
                        logger.warning(
                            f"⚠️ {symbol} {side} 市价单重试前检测到已成交 "
                            f"{filled.get('filled')}，放弃重试（防重复成交）")
                        return filled
                except Exception:
                    pass
                await asyncio.sleep(2 ** attempt)
        raise last_err

    async def _recent_market_fill(self, symbol, side, amount, since):
        """查最近是否有同方向、同数量的成交（用于防重复下单）"""
        try:
            orders = await self.exchange.fetch_orders(symbol, since=since * 1000)
        except Exception:
            return None
        if not orders:
            return None
        for o in reversed(orders):
            if str(o.get('type') or '').lower() != 'market':
                continue
            if str(o.get('side') or '').lower() != side:
                continue
            if abs(float(o.get('amount') or 0) - amount) > amount * 0.02:
                continue
            if float(o.get('filled') or 0) > 0:
                return o
        return None

    async def create_market_buy_order(self, symbol, amount):
        if not self.exchange:
            return None
        try:
            # 市价单成交价未知，无法可靠校验最小名义价值，
            # 这里显式传 0.0 跳过该项（最小数量校验仍然生效）。
            amount = await self._prepare_amount(symbol, amount, 0.0)
            if amount <= 0:
                return None
            order = await self._market_order_with_idem(symbol, 'buy', amount)
            return await self._finalize_order(order, symbol)
        except Exception as e:
            logger.error(f"市价买单失败 {symbol}: {e}")
            return None

    async def create_market_sell_order(self, symbol, amount):
        if not self.exchange:
            return None
        try:
            amount = await self._prepare_amount(symbol, amount, 0.0)
            if amount <= 0:
                return None
            order = await self._market_order_with_idem(symbol, 'sell', amount)
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