import asyncio
import logging
import time
from strategies.base import BaseStrategy
from strategies.signals import (
    SignalEngine, DynamicGridEngine, TrailingStopEngine, AdaptiveEngine
)
from okx_client.rest import OKXRest
from notifier import alert

logger = logging.getLogger(__name__)


DEFAULT_PARAMS = {
    "maxSpend": 100,
    "use_adaptive": True,
    "use_trailing_entry": True,
    "trailing_entry_pct": 0.002,
    "use_trailing_stop": True,
    "use_dynamic_grid": True,
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
    "trend_filter": True,
    "volume_confirm": True,
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
        self._last_score = 0.0

        self._batch_tp_triggered = set()

        # 引擎
        self.signal_engine = SignalEngine()
        self.dynamic_grid = DynamicGridEngine()
        self.trailing_stop = TrailingStopEngine()
        self.adaptive = AdaptiveEngine()
        self._sync_signal_params()

        # 追踪建仓状态
        self._trailing_entry_active = False
        self._trailing_entry_low = 0.0
        self._last_entry_confirm = 0.0

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
                                self.trailing_stop.peak_price = self.avg_buy_price
                        else:
                            self.avg_buy_price = 0.0
                            self.peak_price = 0.0
                            self._batch_tp_triggered.clear()
                            self.trailing_stop.trailing_active = False
                        return
            self.position = 0.0
            self.cost = 0.0
            self.avg_buy_price = 0.0
            self.peak_price = 0.0
            self._batch_tp_triggered.clear()
            self.trailing_stop.trailing_active = False
        except Exception as e:
            logger.error(f"{self.inst_id} 同步持仓失败: {e}")

    async def _record_fee(self):
        try:
            await asyncio.sleep(2.0)
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

    async def _do_market_buy(self, price):
        spend = self.params.get("maxSpend", 100)
        if self._min_sz > 0:
            min_notional = self._min_sz * price
            if spend < min_notional:
                msg = f"{self.inst_id} 买入金额 {spend} < 最小 {min_notional:.2f}"
                logger.warning(msg)
                await alert(f"⚠️ {msg}")
                return

        try:
            r = await asyncio.to_thread(self.rest.market_buy, self.inst_id, spend)
            if r.get("code") == "0":
                data = r.get("data", [])
                filled_sz = 0.0
                avg_px = price
                if data:
                    try:
                        filled_sz = float(data[0].get("accFillSz", 0) or 0)
                        avg_px_raw = data[0].get("avgPx", 0) or 0
                        if float(avg_px_raw) > 0:
                            avg_px = float(avg_px_raw)
                    except (ValueError, TypeError):
                        pass

                if filled_sz <= 0:
                    filled_sz = spend / price

                self.position += filled_sz
                self.cost += filled_sz * avg_px
                self.avg_buy_price = self.cost / self.position if self.position > 0 else avg_px
                self.peak_price = max(self.peak_price, avg_px)
                self.trailing_stop.peak_price = self.peak_price
                self._last_action = ("市价买入", avg_px)

                logger.info(f"{self.inst_id} 买入成功 数量={filled_sz:.6f} 均价={avg_px:.4f} 总持仓={self.position:.6f}")
                await alert(
                    f"✅ {self.inst_id} 买入成功\n"
                    f"花费: {spend} USDT\n"
                    f"数量: {filled_sz:.6f}\n"
                    f"均价: {avg_px:.4f}\n"
                    f"当前持仓: {self.position:.6f}"
                )
                asyncio.create_task(self._record_fee())
            else:
                err = str(r)[:200]
                logger.error(f"{self.inst_id} 买入失败: {r}")
                await alert(f"❌ {self.inst_id} 买入失败\n{err}")
        except Exception as e:
            logger.error(f"{self.inst_id} 买入异常: {e}")
            await alert(f"❌ {self.inst_id} 买入异常: {e}")

    async def _do_market_sell(self, price, size=None, reason=""):
        sell_size = size if size is not None else self.position
        if sell_size <= 0:
            return
        if self._min_sz > 0 and sell_size < self._min_sz:
            msg = f"{self.inst_id} 卖出数量 {sell_size} < 最小 {self._min_sz}"
            logger.warning(msg)
            await alert(f"⚠️ {msg}")
            return

        try:
            r = await asyncio.to_thread(self.rest.market_sell, self.inst_id, sell_size)
            if r.get("code") == "0":
                self._last_action = (f"市价卖出({reason})", price)
                logger.info(f"{self.inst_id} 卖出成功 数量={sell_size:.6f} 价格≈{price:.4f} 原因={reason}")

                if self.avg_buy_price > 0:
                    profit = (price - self.avg_buy_price) * sell_size
                    self.total_profit += profit
                else:
                    profit = 0.0

                self.position -= sell_size
                if self.position < 1e-10:
                    self.position = 0.0
                    self.cost = 0.0
                    self.avg_buy_price = 0.0
                    self.peak_price = 0.0
                    self._batch_tp_triggered.clear()
                    self.trailing_stop.trailing_active = False
                else:
                    self.cost = self.avg_buy_price * self.position

                await alert(
                    f"✅ {self.inst_id} 卖出成功\n"
                    f"数量: {sell_size:.6f}\n"
                    f"价格: ≈{price:.4f}\n"
                    f"原因: {reason}\n"
                    f"本轮盈亏: {profit:+.4f} USDT\n"
                    f"剩余持仓: {self.position:.6f}"
                )
                asyncio.create_task(self._record_fee())
            else:
                err = str(r)[:200]
                logger.error(f"{self.inst_id} 卖出失败: {r}")
                await alert(f"❌ {self.inst_id} 卖出失败\n{err}")
        except Exception as e:
            logger.error(f"{self.inst_id} 卖出异常: {e}")
            await alert(f"❌ {self.inst_id} 卖出异常: {e}")

    async def close_position(self):
        if self.position <= 0:
            return False, "无持仓"
        if self._min_sz > 0 and self.position < self._min_sz:
            return False, f"持仓 {self.position:.8f} < 最小 {self._min_sz}，无法卖出"
        try:
            r = await asyncio.to_thread(self.rest.market_sell, self.inst_id, self.position)
            if r.get("code") == "0":
                data = r.get("data", [])
                avg_px = self._last_price if self._last_price > 0 else self.avg_buy_price
                if data:
                    try:
                        px = data[0].get("avgPx", 0) or 0
                        if float(px) > 0:
                            avg_px = float(px)
                    except (ValueError, TypeError):
                        pass
                profit = 0.0
                if self.avg_buy_price > 0:
                    profit = (avg_px - self.avg_buy_price) * self.position
                    self.total_profit += profit
                sell_size = self.position
                self.position = 0.0
                self.cost = 0.0
                self.avg_buy_price = 0.0
                self.peak_price = 0.0
                self._batch_tp_triggered.clear()
                self.trailing_stop.trailing_active = False
                self._last_action = ("手动清仓", avg_px)
                asyncio.create_task(self._record_fee())
                logger.info(f"{self.inst_id} 清仓成功 数量={sell_size:.6f} 均价={avg_px:.4f} 盈亏={profit:+.4f}")
                return True, f"清仓 {sell_size:.6f} @ {avg_px:.4f}，盈亏 {profit:+.4f} USDT"
            else:
                return False, f"清仓失败: {str(r)[:150]}"
        except Exception as e:
            return False, f"清仓异常: {e}"

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
            self.dynamic_grid.update(closes, highs, lows)
            if self.adaptive.atr > 0:
                self.trailing_stop.update_atr(self.adaptive.atr)

        return self.signal_engine.calculate(closes, highs, lows, volumes)

    async def _check_sell(self, price):
        if self.position <= 0 or self.avg_buy_price <= 0:
            return None
        if price > self.peak_price:
            self.peak_price = price
            self.trailing_stop.peak_price = price

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

        # 追踪止损
        if self.params.get("use_trailing_stop", True):
            ts_reason = self.trailing_stop.check_trailing_stop(price, self.avg_buy_price)
            if ts_reason:
                return {"action": "sell", "reason": ts_reason}

        # ATR动态止损
        if profit_pct <= -self.adaptive.stop_loss_pct:
            return {"action": "sell", "reason": f"ATR止损({profit_pct*100:.2f}%)"}

        return None

    async def on_ticker(self, price, raw):
        if not self.running:
            return
        self._last_price = price

        # 持仓时检查卖出
        if self.position > 0:
            result = await self._check_sell(price)
            if result:
                if result["action"] == "batch_tp":
                    await self._do_market_sell(price, result["sell_size"], reason=result["reason"])
                else:
                    await self._do_market_sell(price, reason=result["reason"])
            return

        # 冷却期
        if time.time() < self._buy_disabled_until:
            return

        ind = await self._get_indicators()
        if not ind:
            return

        use_adaptive = self.params.get("use_adaptive", True)
        if use_adaptive:
            # 用动态网格间距来判断是否处于"低吸"区域
            price = ind.get("close", 0)
            bb_lower = ind.get("bb_lower", 0)
            rsi = ind.get("rsi")

            if self.dynamic_grid.atr <= 0:
                return

            # 动态网格的下轨附近 = 买入区域
            grid_lower = self.dynamic_grid.lower
            in_buy_zone = price <= grid_lower

            # 趋势过滤：趋势市时要求价格更接近下轨
            if self.adaptive.use_trend_filter and ind.get("ema"):
                if price < ind["ema"] * 0.98:
                    in_buy_zone = False

            # RSI 辅助
            if rsi is not None and rsi < 40:
                in_buy_zone = True

            # 布林带下轨辅助
            if bb_lower > 0 and price <= bb_lower * 1.005:
                in_buy_zone = True

            if in_buy_zone:
                # 追踪建仓
                if self.params.get("use_trailing_entry", True):
                    if not self._trailing_entry_active:
                        self._trailing_entry_active = True
                        self._trailing_entry_low = price
                        logger.info(f"{self.inst_id} 进入买入区域，追踪建仓中...")
                    else:
                        if price < self._trailing_entry_low:
                            self._trailing_entry_low = price
                        elif price > self._trailing_entry_low * (1 + self.params.get("trailing_entry_pct", 0.002)):
                            # 价格企稳反弹，确认建仓
                            self._trailing_entry_active = False
                            self._buy_disabled_until = time.time() + 300
                            logger.info(f"{self.inst_id} 追踪建仓确认，执行买入")
                            await self._do_market_buy(price)
                else:
                    self._buy_disabled_until = time.time() + 300
                    await self._do_market_buy(price)
        else:
            # 固定模式回退
            if self.signal_engine.buy_signal(
                ind,
                use_trend_filter=self.params.get("trend_filter", True),
                use_volume=self.params.get("volume_confirm", True),
            ):
                self._buy_disabled_until = time.time() + 300
                await self._do_market_buy(price)

    async def start(self):
        self.running = True
        await self._load_instrument_rules()
        await self._sync_position_from_okx()
        logger.info(f"{self.inst_id} 策略已启动（动态网格+追踪建仓+追踪止损）")

    async def stop(self):
        self.running = False
        self._trailing_entry_active = False

    async def get_signal_forecast(self):
        ind = await self._get_indicators()
        if not ind:
            return {"inst_id": self.inst_id, "error": "K线数据不足"}

        price = ind.get("close", 0)
        grid_lower = self.dynamic_grid.lower
        grid_upper = self.dynamic_grid.upper
        spacing = self.dynamic_grid.spacing
        atr = self.dynamic_grid.atr
        regime = self.dynamic_grid.regime

        # 计算距离买入区的百分比
        if grid_lower > 0 and price > 0:
            distance_to_buy = (price - grid_lower) / grid_lower * 100
        else:
            distance_to_buy = 0

        in_buy_zone = price <= grid_lower
        rsi = ind.get("rsi", 50)

        # 拦截原因
        blockers = []
        if not self.running:
            blockers.append("策略未启动")
        if self._trailing_entry_active:
            blockers.append(f"追踪建仓中（低点 {self._trailing_entry_low:.4f}，等待反弹确认）")
        if time.time() < self._buy_disabled_until:
            wait = int(self._buy_disabled_until - time.time())
            blockers.append(f"冷却中，还需 {wait} 秒")
        if not in_buy_zone:
            blockers.append(f"价格距网格下轨还有 {distance_to_buy:.2f}%")
        if self.adaptive.use_trend_filter and ind.get("ema") and price < ind["ema"] * 0.98:
            blockers.append(f"趋势过滤：价格低于EMA200的98%")

        # 状态
        if in_buy_zone and not blockers:
            status = "✅ 买入区域"
        elif in_buy_zone:
            status = "🟡 已到区域有拦截"
        elif distance_to_buy < 1:
            status = "🔥 接近买入区"
        elif distance_to_buy < 3:
            status = "⚡ 中等距离"
        else:
            status = "⏳ 等待"

        return {
            "inst_id": self.inst_id,
            "bar": self.params.get("bar", "15m"),
            "price": price,
            "status": status,
            "grid_lower": grid_lower,
            "grid_upper": grid_upper,
            "spacing_pct": spacing * 100,
            "atr": atr,
            "regime": regime,
            "distance_to_buy": distance_to_buy,
            "in_buy_zone": in_buy_zone,
            "rsi": rsi,
            "blockers": blockers,
            "trailing_entry_active": self._trailing_entry_active,
            "trailing_entry_low": self._trailing_entry_low,
            "running": self.running,
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
            "bar": self.params.get("bar", "15m"),
            "use_adaptive": self.params.get("use_adaptive", True),
            "regime": self.adaptive.regime,
            "adx": self.adaptive.adx,
            "atr": self.adaptive.atr,
            "batch_tp_triggered": len(self._batch_tp_triggered),
            "trailing_entry_active": self._trailing_entry_active,
        })
        return s