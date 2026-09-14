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

        self.signal_engine = SignalEngine()
        self._kline_cache = []
        self._last_kline_ts = 0
        self._last_price = 0.0
        self._buy_disabled_until = 0.0
        self._min_notional = 0.0
        self._lot_sz = 0.0
        self._min_sz = 0.0

    async def _load_instrument_rules(self):
        """加载交易对的最小下单规格"""
        try:
            resp = await asyncio.to_thread(self.rest.get_instruments, "SPOT", self.inst_id)
            if resp.get("code") == "0" and resp.get("data"):
                inst = resp["data"][0]
                self._min_sz = float(inst.get("minSz", 0))
                self._lot_sz = float(inst.get("lotSz", 0.00000001))
                logger.info(f"{self.inst_id} 规格: minSz={self._min_sz}, lotSz={self._lot_sz}")
        except Exception as e:
            logger.error(f"{self.inst_id} 加载规格失败: {e}")

    async def _check_min_order(self, price):
        """校验单次投入是否满足最小下单要求"""
        if self._min_sz <= 0:
            await self._load_instrument_rules()
        spend = self.params.get("maxSpend", 100)
        min_notional = self._min_sz * price
        if spend < min_notional:
            return False, f"投入 {spend} USDT < 最小 {min_notional:.2f} USDT"
        return True, ""

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

    async def _do_buy(self, price, tag):
        # 最小下单校验
        ok, msg = await self._check_min_order(price)
        if not ok:
            logger.warning(f"{self.inst_id} 买入跳过: {msg}")
            return

        spend = self.params.get("maxSpend", 100)
        try:
            r = await asyncio.to_thread(self.rest.market_buy, self.inst_id, spend)
            if r.get("code") == "0":
                self._last_action = (tag, price)
                logger.info(f"{self.inst_id} 买入 @ {price} ({tag})")
                await self._record_fee()
                await self._sync_position_from_okx()
            else:
                logger.error(f"{self.inst_id} 买入失败: {r}")
        except Exception as e:
            logger.error(f"{self.inst_id} 买入异常: {e}")

    async def _do_sell(self, price, tag):
        sell_size = self.position
        sell_cost = self.cost
        if sell_size <= 0:
            return
        # 卖出数量必须 >= minSz
        if self._min_sz > 0 and sell_size < self._min_sz:
            logger.warning(f"{self.inst_id} 持仓 {sell_size} < 最小 {self._min_sz},无法卖出")
            return
        try:
            r = await asyncio.to_thread(self.rest.market_sell, self.inst_id, sell_size)
            if r.get("code") == "0":
                self._last_action = (tag, price)
                profit = price * sell_size - sell_cost
                self.total_profit += profit
                logger.info(f"{self.inst_id} 卖出 @ {price} ({tag}), 获利 {profit:.4f}")
                await self._record_fee()
                await self._sync_position_from_okx()
                self._buy_disabled_until = time.time() + 600
            else:
                logger.error(f"{self.inst_id} 卖出失败: {r}")
        except Exception as e:
            logger.error(f"{self.inst_id} 卖出异常: {e}")

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

        if self.position > 0:
            reason = await self._check_sell(price)
            if reason:
                await self._do_sell(price, reason)
            return

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
            await self._do_buy(price, "信号")

    async def start(self):
        self.running = True
        await self._load_instrument_rules()
        await self._sync_position_from_okx()
        logger.info(f"{self.inst_id} 全自动低吸高卖已启动")

    async def stop(self):
        self.running = False

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
            "min_sz": self._min_sz,
            "lot_sz": self._lot_sz,
        })
        return s