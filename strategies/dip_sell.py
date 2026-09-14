import asyncio
import logging
import time
from strategies.base import BaseStrategy
from strategies.signals import SignalEngine
from okx_client.rest import OKXRest

logger = logging.getLogger(__name__)


class DipSellStrategy(BaseStrategy):
    def __init__(self, inst_id, params=None):
        default = {
            "maxSpend": 100,
            "stop_loss_pct": 0.05,
            "take_profit_pct": 0.03,
            "use_trailing": True,
            "trailing_pct": 0.02,
            "trend_filter": True,
            "volume_confirm": True,
            "limit_offset_pct": 0.002,   # 限价单挂单偏移
        }
        merged = {**default, **(params or {})}
        super().__init__(inst_id, merged)

        self.rest = OKXRest()
        self.position = 0.0
        self.cost = 0.0
        self.avg_buy_price = 0.0
        self.peak_price = 0.0
        self.total_profit = 0.0
        self.total_fee = 0.0
        self.trade_count = 0
        self._last_action = None

        # 挂单状态
        self._pending_buy_ord_id = None
        self._pending_sell_ord_id = None

        self.signal_engine = SignalEngine()
        self._kline_cache = []
        self._last_kline_ts = 0
        self._last_price = 0.0
        self._buy_disabled_until = 0.0
        self._min_sz = 0.0
        self._lot_sz = 0.0

    async def _load_instrument_rules(self):
        try:
            resp = await asyncio.to_thread(self.rest.get_instruments, "SPOT", self.inst_id)
            if resp.get("code") == "0" and resp.get("data"):
                inst = resp["data"][0]
                self._min_sz = float(inst.get("minSz", 0))
                self._lot_sz = float(inst.get("lotSz", 0.00000001))
                logger.info(f"{self.inst_id} 规格: minSz={self._min_sz}, lotSz={self._lot_sz}")
        except Exception as e:
            logger.error(f"{self.inst_id} 加载规格失败: {e}")

    async def _restore_pending_orders(self):
        """启动时从 OKX 恢复未成交挂单状态"""
        try:
            resp = await asyncio.to_thread(self.rest.get_pending_orders, self.inst_id)
            if resp.get("code") != "0":
                return
            for order in resp.get("data", []):
                side = order.get("side")
                ord_id = order.get("ordId")
                px = float(order.get("px", 0))
                sz = float(order.get("sz", 0))
                if side == "buy":
                    self._pending_buy_ord_id = ord_id
                    logger.info(f"{self.inst_id} 恢复挂单买入 {ord_id} @ {px} x {sz}")
                elif side == "sell":
                    self._pending_sell_ord_id = ord_id
                    logger.info(f"{self.inst_id} 恢复挂单卖出 {ord_id} @ {px} x {sz}")
        except Exception as e:
            logger.error(f"{self.inst_id} 恢复挂单失败: {e}")

    async def _fetch_klines(self):
        try:
            resp = await asyncio.to_thread(self.rest.get_candles, self.inst_id, "1H", 300)
            if resp.get("code") == "0" and resp.get("data"):
                self._kline_cache = list(reversed(resp["data"]))
        except Exception as e:
            logger.error(f"{self.inst_id} 获取K线失败: {e}")

    async def _sync_position_from_okx(self):
        try:
            base_ccy = self.inst_id.split("-")[0]
            bal_resp = await asyncio.to_thread(self.rest.get_balance, "USDT")
            if bal_resp.get("code") == "0" and bal_resp.get("data"):
                details = bal_resp["data"][0].get("details", [])
                for d in details:
                    if d.get("ccy") == base_ccy:
                        prev_pos = self.position
                        self.position = float(d.get("eq", 0))
                        self.cost = float(d.get("eqUsd", 0))
                        if self.position > 0:
                            self.avg_buy_price = self.cost / self.position
                            if prev_pos <= 0:
                                self.peak_price = self.avg_buy_price
                        else:
                            self.avg_buy_price = 0.0
                            self.peak_price = 0.0
                        return
            self.position = 0.0
            self.cost = 0.0
            self.avg_buy_price = 0.0
            self.peak_price = 0.0
        except Exception as e:
            logger.error(f"{self.inst_id} 同步持仓失败: {e}")

    async def _record_fee(self):
        try:
            await asyncio.sleep(1.5)
            fills = await asyncio.to_thread(self.rest.get_fills, "SPOT", self.inst_id, 20)
            if fills.get("code") != "0":
                return
            now_ms = int(time.time() * 1000)
            for f in fills.get("data", []):
                try:
                    ts = int(f.get("ts", 0))
                    if now_ms - ts > 30000:
                        continue
                    fee = abs(float(f.get("fee", 0)))
                    self.total_fee += fee
                    self.trade_count += 1
                except (ValueError, TypeError):
                    continue
        except Exception as e:
            logger.error(f"{self.inst_id} 记录手续费失败: {e}")

    async def _do_limit_buy(self, price):
        """限价买入（Maker）"""
        offset = self.params.get("limit_offset_pct", 0.002)
        buy_price = round(price * (1 - offset), 6)

        spend = self.params.get("maxSpend", 100)
        size = spend / buy_price
        # 对齐 lotSz
        if self._lot_sz > 0:
            size = round(size / self._lot_sz) * self._lot_sz
        if size < self._min_sz:
            logger.warning(f"{self.inst_id} 买入数量 {size} < 最小 {self._min_sz}")
            return

        try:
            r = await asyncio.to_thread(self.rest.limit_buy, self.inst_id, buy_price, size)
            if r.get("code") == "0":
                self._pending_buy_ord_id = r["data"][0]["ordId"]
                self._last_action = ("限价买入挂单", buy_price)
                logger.info(f"{self.inst_id} 限价买入挂单 @ {buy_price}, size={size}")
        except Exception as e:
            logger.error(f"{self.inst_id} 限价买入失败: {e}")

    async def _do_limit_sell(self, price):
        """限价卖出（Maker）"""
        sell_size = self.position
        if sell_size <= 0:
            return
        if self._min_sz > 0 and sell_size < self._min_sz:
            logger.warning(f"{self.inst_id} 持仓 {sell_size} < 最小 {self._min_sz}")
            return

        offset = self.params.get("limit_offset_pct", 0.002)
        sell_price = round(price * (1 + offset), 6)

        try:
            r = await asyncio.to_thread(self.rest.limit_sell, self.inst_id, sell_price, sell_size)
            if r.get("code") == "0":
                self._pending_sell_ord_id = r["data"][0]["ordId"]
                self._last_action = ("限价卖出挂单", sell_price)
                logger.info(f"{self.inst_id} 限价卖出挂单 @ {sell_price}, size={sell_size}")
        except Exception as e:
            logger.error(f"{self.inst_id} 限价卖出失败: {e}")

    async def _get_indicators(self):
        now = time.time()
        if now - self._last_kline_ts > 60:
            await self._fetch_klines()
            self._last_kline_ts = now
        if len(self._kline_cache) < 50:
            return None
        closes = [float(k[4]) for k in self._kline_cache]
        highs = [float(k[2]) for k in self._kline_cache]
        lows = [float(k[3]) for k in self._kline_cache]
        volumes = [float(k[5]) for k in self._kline_cache]
        return self.signal_engine.calculate(closes, highs, lows, volumes)

    async def _check_sell(self, price):
        if self.position <= 0 or self.avg_buy_price <= 0:
            return None
        if price > self.peak_price:
            self.peak_price = price
        profit_pct = (price - self.avg_buy_price) / self.avg_buy_price

        if self.params.get("use_trailing", True) and self.peak_price > self.avg_buy_price:
            drawdown = (self.peak_price - price) / self.peak_price
            trailing_pct = self.params.get("trailing_pct", 0.02)
            if profit_pct > 0.005 and drawdown >= trailing_pct:
                return f"移动止盈(回撤{drawdown*100:.2f}%)"

        if profit_pct >= self.params.get("take_profit_pct", 0.03):
            return f"止盈({profit_pct*100:.2f}%)"

        if profit_pct <= -self.params.get("stop_loss_pct", 0.05):
            return f"止损({profit_pct*100:.2f}%)"

        ind = await self._get_indicators()
        if ind and self.signal_engine.sell_signal(ind):
            return "信号"
        return None

    async def on_ticker(self, price, raw):
        if not self.running:
            return
        self._last_price = price

        # 持仓时检查卖出
        if self.position > 0:
            if self._pending_sell_ord_id:
                return  # 已有挂单，等待成交
            reason = await self._check_sell(price)
            if reason:
                await self._do_limit_sell(price)
            return

        # 空仓时检查买入
        if self._pending_buy_ord_id:
            return  # 已有挂单，等待成交

        if time.time() < self._buy_disabled_until:
            return

        ind = await self._get_indicators()
        if not ind:
            return

        if self.signal_engine.buy_signal(
            ind,
            use_trend_filter=self.params.get("trend_filter", True),
            use_volume=self.params.get("volume_confirm", True),
        ):
            await self._do_limit_buy(price)

    async def start(self):
        self.running = True
        await self._load_instrument_rules()
        await self._restore_pending_orders()
        await self._sync_position_from_okx()
        logger.info(f"{self.inst_id} 全自动低吸高卖已启动（限价单模式）")

    async def stop(self):
        self.running = False
        # 撤销未成交挂单
        for ord_id in [self._pending_buy_ord_id, self._pending_sell_ord_id]:
            if ord_id:
                try:
                    await asyncio.to_thread(self.rest.cancel_order, self.inst_id, ord_id)
                except Exception as e:
                    logger.error(f"撤单失败: {e}")
        self._pending_buy_ord_id = None
        self._pending_sell_ord_id = None

    def snapshot(self):
        s = super().snapshot()
        s.update({
            "position": self.position,
            "avg_buy_price": self.avg_buy_price,
            "peak_price": self.peak_price,
            "last_action": self._last_action,
            "total_profit": self.total_profit,
            "total_fee": self.total_fee,
            "trade_count": self.trade_count,
            "pending_buy": self._pending_buy_ord_id,
            "pending_sell": self._pending_sell_ord_id,
        })
        return s