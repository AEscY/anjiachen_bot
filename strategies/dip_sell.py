import asyncio
import logging
import time
from strategies.base import BaseStrategy
from strategies.signals import SignalEngine, AdaptiveEngine
from okx_client.rest import OKXRest

logger = logging.getLogger(__name__)

DEFAULT_PARAMS = {
    "maxSpend": 100,
    "use_adaptive": True,
    "use_trailing": True,
    "limit_offset_pct": 0.002,
    "bar": "15m",
    "rsi_period": 14,
    "bb_period": 20,
    "macd_fast": 12,
    "macd_slow": 26,
    "macd_signal": 9,
    "ema_period": 200,
    "vol_ma_period": 20,
    "vol_multiplier": 1.5,
    "stop_loss_pct": 0.05,
    "take_profit_pct": 0.03,
    "trailing_pct": 0.02,
}

BATCH_TP_LEVELS = [0.03, 0.06, 0.10]
BATCH_TP_RATIOS = [0.30, 0.30, 0.40]


class DipSellStrategy(BaseStrategy):
    def __init__(self, inst_id, params=None):
        merged = {**DEFAULT_PARAMS, **(params or {})}
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
        self._pending_buy_ord_id = None
        self._pending_sell_ord_id = None
        self._pending_buy_price = None
        self._batch_tp_triggered = set()
        self.signal_engine = SignalEngine()
        self.adaptive = AdaptiveEngine()
        self._sync_signal_params()
        self._kline_cache = []
        self._last_kline_ts = 0
        self._last_price = 0.0
        self._buy_disabled_until = 0.0
        self._min_sz = 0.0
        self._lot_sz = 0.0
        # 追踪建仓相关
        self._trailing_entry_active = False
        self._trailing_entry_low = 0.0

    def _sync_signal_params(self):
        p = self.params
        self.signal_engine.rsi_period = p.get("rsi_period", 14)
        self.signal_engine.bb_period = p.get("bb_period", 20)
        self.signal_engine.macd_fast = p.get("macd_fast", 12)
        self.signal_engine.macd_slow = p.get("macd_slow", 26)
        self.signal_engine.macd_signal = p.get("macd_signal", 9)
        self.signal_engine.ema_period = p.get("ema_period", 200)
        self.signal_engine.vol_ma_period = p.get("vol_ma_period", 20)
        self.signal_engine.vol_multiplier = p.get("vol_multiplier", 1.5)

    async def set_param(self, key, value):
        self.params[key] = value
        self._sync_signal_params()
        self._last_kline_ts = 0
        self._kline_cache = []

    async def reset_params(self):
        self.params = {**DEFAULT_PARAMS}
        self._sync_signal_params()
        self._last_kline_ts = 0
        self._kline_cache = []

    async def _load_instrument_rules(self):
        try:
            resp = await asyncio.to_thread(self.rest.get_instruments, "SPOT", self.inst_id)
            if resp.get("code") == "0" and resp.get("data"):
                inst = resp["data"][0]
                self._min_sz = float(inst.get("minSz", 0))
                self._lot_sz = float(inst.get("lotSz", 0.00000001))
        except Exception as e:
            logger.error(f"{self.inst_id} 加载规格失败: {e}")

    async def _fetch_klines(self):
        bar = self.params.get("bar", "15m")
        try:
            resp = await asyncio.to_thread(self.rest.get_candles, self.inst_id, bar, 300)
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
                            self._batch_tp_triggered.clear()
                        return
            self.position = 0.0
            self.cost = 0.0
            self.avg_buy_price = 0.0
            self.peak_price = 0.0
            self._batch_tp_triggered.clear()
        except Exception as e:
            logger.error(f"{self.inst_id} 同步持仓失败: {e}")

    async def _get_indicators(self):
        now = time.time()
        if now - self._last_kline_ts > 30:
            await self._fetch_klines()
            self._last_kline_ts = now
        if len(self._kline_cache) < 50:
            return None
        closes = [float(k[4]) for k in self._kline_cache]
        highs = [float(k[2]) for k in self._kline_cache]
        lows = [float(k[3]) for k in self._kline_cache]
        volumes = [float(k[5]) for k in self._kline_cache]
        if self.params.get("use_adaptive", True):
            self.adaptive.update(closes, highs, lows)
            self.signal_engine.rsi_oversold = self.adaptive.rsi_oversold
            self.signal_engine.rsi_overbought = self.adaptive.rsi_overbought
        return self.signal_engine.calculate(closes, highs, lows, volumes)

    async def _do_limit_buy(self, price):
        offset = self.params.get("limit_offset_pct", 0.002)
        buy_price = round(price * (1 - offset), 6)
        spend = self.params.get("maxSpend", 100)
        size = spend / buy_price
        if self._lot_sz > 0:
            size = round(size / self._lot_sz) * self._lot_sz
        if self._min_sz > 0 and size < self._min_sz:
            logger.warning(f"{self.inst_id} 买入数量 {size} < 最小 {self._min_sz}")
            return
        try:
            r = await asyncio.to_thread(self.rest.limit_buy, self.inst_id, buy_price, size)
            if r.get("code") == "0":
                self._pending_buy_ord_id = r["data"][0]["ordId"]
                self._pending_buy_price = buy_price
                self._last_action = ("限价买入挂单", buy_price)
                logger.info(f"{self.inst_id} 限价买入挂单 @ {buy_price}, size={size}")
        except Exception as e:
            logger.error(f"{self.inst_id} 限价买入失败: {e}")

    async def _do_limit_sell(self, price, size=None):
        sell_size = size if size is not None else self.position
        if sell_size <= 0:
            return
        if self._min_sz > 0 and sell_size < self._min_sz:
            logger.warning(f"{self.inst_id} 卖出数量 {sell_size} < 最小 {self._min_sz}")
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

    async def _check_sell(self, price):
        if self.position <= 0 or self.avg_buy_price <= 0:
            return None
        if price > self.peak_price:
            self.peak_price = price
        profit_pct = (price - self.avg_buy_price) / self.avg_buy_price
        # 分批止盈
        for i, (tp_level, ratio) in enumerate(zip(BATCH_TP_LEVELS, BATCH_TP_RATIOS)):
            if i in self._batch_tp_triggered:
                continue
            if profit_pct >= tp_level:
                self._batch_tp_triggered.add(i)
                sell_size = self.position * ratio
                if i == len(BATCH_TP_LEVELS) - 1:
                    sell_size = self.position
                if self._min_sz > 0 and sell_size < self._min_sz:
                    continue
                return {"action": "batch_tp", "reason": f"分批止盈{i+1}档({tp_level*100:.0f}%)", "sell_size": sell_size}
        # 移动止盈
        sl_pct = self.adaptive.stop_loss_pct
        tp_pct = self.adaptive.take_profit_pct
        tr_pct = self.adaptive.trailing_pct
        if self.params.get("use_trailing", True) and self.peak_price > self.avg_buy_price:
            drawdown = (self.peak_price - price) / self.peak_price
            if profit_pct > 0.005 and drawdown >= tr_pct:
                return {"action": "sell", "reason": f"移动止盈(回撤{drawdown*100:.2f}%)"}
        if not self._batch_tp_triggered and profit_pct >= tp_pct:
            return {"action": "sell", "reason": f"止盈({profit_pct*100:.2f}%)"}
        if profit_pct <= -sl_pct:
            return {"action": "sell", "reason": f"止损({profit_pct*100:.2f}%)"}
        return None

    async def on_ticker(self, price, raw):
        if not self.running:
            return
        self._last_price = price

        # 持仓时检查卖出
        if self.position > 0:
            if self._pending_sell_ord_id:
                return
            result = await self._check_sell(price)
            if result:
                if result["action"] == "batch_tp":
                    await self._do_limit_sell(price, result["sell_size"])
                else:
                    await self._do_limit_sell(price)
            return

        # 挂单中等待成交
        if self._pending_buy_ord_id:
            return
        if time.time() < self._buy_disabled_until:
            return

        ind = await self._get_indicators()
        if not ind:
            return

        # 追踪建仓逻辑
        if self._trailing_entry_active:
            if price < self._trailing_entry_low:
                self._trailing_entry_low = price
            elif price > self._trailing_entry_low * 1.002:
                self._trailing_entry_active = False
                await self._do_limit_buy(price)
            return

        # 信号触发
        if self.signal_engine.buy_signal(ind):
            logger.info(
                f"{self.inst_id} 买入信号 价格={price:.2f} RSI={ind.get('rsi', 0):.1f} "
                f"体制={self.adaptive.regime} ADX={self.adaptive.adx:.1f}"
            )
            self._trailing_entry_active = True
            self._trailing_entry_low = price

    async def start(self):
        self.running = True
        await self._load_instrument_rules()
        await self._sync_position_from_okx()
        logger.info(f"{self.inst_id} 低吸高卖已启动，周期 {self.params.get('bar')}")

    async def stop(self):
        self.running = False
        for ord_id in [self._pending_buy_ord_id, self._pending_sell_ord_id]:
            if ord_id:
                try:
                    await asyncio.to_thread(self.rest.cancel_order, self.inst_id, ord_id)
                except Exception as e:
                    logger.error(f"撤单失败: {e}")
        self._pending_buy_ord_id = None
        self._pending_sell_ord_id = None
        self._pending_buy_price = None

    async def get_signal_forecast(self):
        ind = await self._get_indicators()
        if not ind:
            return {"inst_id": self.inst_id, "error": "K线数据不足"}
        rsi = ind.get("rsi")
        bb_lower = ind.get("bb_lower")
        price = ind.get("close")
        macd_ok = (ind.get("macd_hist") or 0) > 0
        mean_reversion = (rsi is not None and rsi < 45) or (price and bb_lower and price <= bb_lower)
        return {
            "inst_id": self.inst_id,
            "bar": self.params.get("bar", "15m"),
            "price": price,
            "rsi": rsi,
            "bb_lower": bb_lower,
            "macd_ok": macd_ok,
            "mean_reversion_signal": mean_reversion,
            "regime": self.adaptive.regime,
            "adx": self.adaptive.adx,
            "atr": self.adaptive.atr,
            "buy_ready": mean_reversion,
        }

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
            "bar": self.params.get("bar", "15m"),
            "use_adaptive": self.params.get("use_adaptive", True),
            "regime": self.adaptive.regime,
            "adx": self.adaptive.adx,
            "atr": self.adaptive.atr,
            "batch_tp_triggered": len(self._batch_tp_triggered),
        })
        return s