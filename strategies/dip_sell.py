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
    "trend_filter": True,
    "volume_confirm": True,
    "limit_offset_pct": 0.002,
    "bar": "15m",
    "rsi_period": 14,
    "rsi_oversold": 30,
    "rsi_overbought": 70,
    "bb_period": 20,
    "bb_std": 2.0,
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
        self._last_score = 0.0

        self._pending_buy_ord_id = None
        self._pending_sell_ord_id = None

        self.signal_engine = SignalEngine()
        self.adaptive = AdaptiveEngine()
        self._sync_signal_params()

        self._kline_cache = []
        self._last_kline_ts = 0
        self._last_price = 0.0
        self._buy_disabled_until = 0.0
        self._min_sz = 0.0
        self._lot_sz = 0.0

    def _sync_signal_params(self):
        p = self.params
        self.signal_engine.rsi_period = p.get("rsi_period", 14)
        self.signal_engine.rsi_oversold = p.get("rsi_oversold", 30)
        self.signal_engine.rsi_overbought = p.get("rsi_overbought", 70)
        self.signal_engine.bb_period = p.get("bb_period", 20)
        self.signal_engine.bb_std = p.get("bb_std", 2.0)
        self.signal_engine.macd_fast = p.get("macd_fast", 12)
        self.signal_engine.macd_slow = p.get("macd_slow", 26)
        self.signal_engine.macd_signal = p.get("macd_signal", 9)
        self.signal_engine.ema_period = p.get("ema_period", 200)
        self.signal_engine.vol_ma_period = p.get("vol_ma_period", 20)
        self.signal_engine.vol_multiplier = p.get("vol_multiplier", 1.5)

    def _effective(self, key):
        """获取生效参数：自适应开启时优先使用自适应值"""
        if self.params.get("use_adaptive", True):
            if key == "rsi_oversold":
                return self.adaptive.rsi_oversold
            if key == "rsi_overbought":
                return self.adaptive.rsi_overbought
            if key == "bb_std":
                return self.adaptive.bb_std
            if key == "stop_loss_pct":
                return self.adaptive.stop_loss_pct
            if key == "take_profit_pct":
                return self.adaptive.take_profit_pct
            if key == "trailing_pct":
                return self.adaptive.trailing_pct
        return self.params.get(key)

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
                logger.info(f"{self.inst_id} 规格: minSz={self._min_sz}, lotSz={self._lot_sz}")
        except Exception as e:
            logger.error(f"{self.inst_id} 加载规格失败: {e}")

    async def _restore_pending_orders(self):
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
                self._last_action = ("限价买入挂单", buy_price)
                logger.info(f"{self.inst_id} 限价买入挂单 @ {buy_price}, size={size}")
        except Exception as e:
            logger.error(f"{self.inst_id} 限价买入失败: {e}")

    async def _do_limit_sell(self, price):
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
        if now - self._last_kline_ts > 30:
            await self._fetch_klines()
            self._last_kline_ts = now
        if len(self._kline_cache) < 50:
            return None
        closes = [float(k[4]) for k in self._kline_cache]
        highs = [float(k[2]) for k in self._kline_cache]
        lows = [float(k[3]) for k in self._kline_cache]
        volumes = [float(k[5]) for k in self._kline_cache]

        # 更新自适应引擎
        if self.params.get("use_adaptive", True):
            self.adaptive.update(closes, highs, lows)
            # 同步 RSI / BB 阈值到 signal_engine
            self.signal_engine.rsi_oversold = self.adaptive.rsi_oversold
            self.signal_engine.rsi_overbought = self.adaptive.rsi_overbought
            self.signal_engine.bb_std = self.adaptive.bb_std

        return self.signal_engine.calculate(closes, highs, lows)

    async def _check_sell(self, price):
        if self.position <= 0 or self.avg_buy_price <= 0:
            return None
        if price > self.peak_price:
            self.peak_price = price
        profit_pct = (price - self.avg_buy_price) / self.avg_buy_price

        sl_pct = self._effective("stop_loss_pct")
        tp_pct = self._effective("take_profit_pct")
        tr_pct = self._effective("trailing_pct")

        # 移动止盈优先
        if self.params.get("use_trailing", True) and self.peak_price > self.avg_buy_price:
            drawdown = (self.peak_price - price) / self.peak_price
            if profit_pct > 0.005 and drawdown >= tr_pct:
                return f"移动止盈(回撤{drawdown*100:.2f}%)"

        # 固定止盈
        if profit_pct >= tp_pct:
            return f"止盈({profit_pct*100:.2f}%)"

        # 止损
        if profit_pct <= -sl_pct:
            return f"止损({profit_pct*100:.2f}%)"

        # 信号卖出（分数制）
        ind = await self._get_indicators()
        if ind:
            sell_score = self.adaptive.calc_sell_score(ind)
            if sell_score >= self.adaptive.sell_threshold:
                return f"信号(分数{sell_score:.0f})"
        return None

    async def on_ticker(self, price, raw):
        if not self.running:
            return
        self._last_price = price

        if self.position > 0:
            if self._pending_sell_ord_id:
                return
            reason = await self._check_sell(price)
            if reason:
                await self._do_limit_sell(price)
            return

        if self._pending_buy_ord_id:
            return

        if time.time() < self._buy_disabled_until:
            return

        ind = await self._get_indicators()
        if not ind:
            return

        # 分数制买入
        use_adaptive = self.params.get("use_adaptive", True)
        if use_adaptive:
            score = self.adaptive.calc_buy_score(ind)
            self._last_score = score
            # 趋势过滤
            if self.params.get("trend_filter", True) and ind.get("ema") is not None:
                if ind["close"] < ind["ema"]:
                    return
            if score >= self.adaptive.buy_threshold:
                logger.info(f"{self.inst_id} 买入信号分数 {score:.0f} >= {self.adaptive.buy_threshold}")
                await self._do_limit_buy(price)
        else:
            # 保留老的布尔条件模式
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
        mode = "自适应模式" if self.params.get("use_adaptive", True) else "固定参数模式"
        logger.info(f"{self.inst_id} 低吸高卖已启动（{mode}），周期 {self.params.get('bar')}")

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

    async def get_signal_status(self):
        ind = await self._get_indicators()
        if not ind:
            return {"inst_id": self.inst_id, "error": "K线数据不足"}

        use_adaptive = self.params.get("use_adaptive", True)
        buy_score = self.adaptive.calc_buy_score(ind) if use_adaptive else 0
        sell_score = self.adaptive.calc_sell_score(ind) if use_adaptive else 0

        return {
            "inst_id": self.inst_id,
            "bar": self.params.get("bar", "15m"),
            "price": ind["close"],
            "rsi": ind["rsi"],
            "rsi_oversold": self.adaptive.rsi_oversold if use_adaptive else self.params.get("rsi_oversold", 30),
            "bb_lower": ind["bb_lower"],
            "macd_ok": (ind.get("macd_hist") or 0) > 0 or (
                ind.get("macd") is not None and ind.get("macd_signal") is not None and ind["macd"] > ind["macd_signal"]
            ),
            "buy_score": buy_score,
            "buy_threshold": self.adaptive.buy_threshold if use_adaptive else 0,
            "volatility": self.adaptive.volatility if use_adaptive else 0,
            "buy_ready": (buy_score >= self.adaptive.buy_threshold) if use_adaptive else False,
            "use_adaptive": use_adaptive,
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
            "last_score": self._last_score,
            "adaptive_vol": self.adaptive.volatility,
            "adaptive_sl": self.adaptive.stop_loss_pct,
            "adaptive_tp": self.adaptive.take_profit_pct,
            "adaptive_rsi_os": self.adaptive.rsi_oversold,
            "adaptive_threshold": self.adaptive.buy_threshold,
        })
        return s