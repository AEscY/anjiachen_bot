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
            "basePx": 0,
            "buyPct": 0.98,
            "sellPct": 1.03,
            "maxSpend": 100,
            "use_signal": False,
            "use_trend_filter": False,
            "use_volume": False,
        }
        merged = {**default, **(params or {})}
        super().__init__(inst_id, merged)

        self.rest = OKXRest()
        self.base_px = merged["basePx"]
        self.buy_px = self.base_px * merged["buyPct"]
        self.sell_px = self.base_px * merged["sellPct"]
        self.position = 0.0
        self.cost = 0.0
        self._last_action = None
        self.total_profit = 0.0
        self.total_fee = 0.0
        self.trade_count = 0

        self.signal_engine = SignalEngine()
        self._kline_cache = []
        self._last_kline_ts = 0

    async def _fetch_klines(self):
        try:
            resp = await asyncio.to_thread(
                self.rest.get_candles, self.inst_id, "1H", 300
            )
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
                        self.position = float(d.get("eq", 0))
                        self.cost = float(d.get("eqUsd", 0))
                        return
            self.position = 0.0
            self.cost = 0.0
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
        spend = self.params.get("maxSpend", 100)
        try:
            r = await asyncio.to_thread(self.rest.market_buy, self.inst_id, spend)
            if r.get("code") == "0":
                self._last_action = (tag, price)
                logger.info(f"{self.inst_id} 买入 @ {price} ({tag})")
                await self._record_fee()
                await self._sync_position_from_okx()
        except Exception as e:
            logger.error(f"{self.inst_id} 买入失败: {e}")

    async def _do_sell(self, price, tag):
        sell_size = self.position
        sell_cost = self.cost
        try:
            r = await asyncio.to_thread(self.rest.market_sell, self.inst_id, sell_size)
            if r.get("code") == "0":
                self._last_action = (tag, price)
                profit = price * sell_size - sell_cost
                self.total_profit += profit
                logger.info(f"{self.inst_id} 卖出 @ {price} ({tag}), 获利 {profit:.4f}")
                await self._record_fee()
                await self._sync_position_from_okx()
        except Exception as e:
            logger.error(f"{self.inst_id} 卖出失败: {e}")

    async def _on_ticker_signal(self, price):
        now = time.time()
        if now - self._last_kline_ts > 60:
            await self._fetch_klines()
            self._last_kline_ts = now

        if len(self._kline_cache) < 50:
            return

        closes = [float(k[4]) for k in self._kline_cache]
        highs = [float(k[2]) for k in self._kline_cache]
        lows = [float(k[3]) for k in self._kline_cache]
        volumes = [float(k[5]) for k in self._kline_cache]

        ind = self.signal_engine.calculate(closes, highs, lows, volumes)

        if self.position <= 0:
            if self.signal_engine.buy_signal(
                ind,
                use_trend_filter=self.params.get("use_trend_filter", False),
                use_volume=self.params.get("use_volume", False),
            ):
                await self._do_buy(price, "buy_signal")

        elif self.position > 0:
            if self.signal_engine.sell_signal(ind):
                await self._do_sell(price, "sell_signal")

    async def _on_ticker_threshold(self, price):
        if self.position <= 0 and price <= self.buy_px:
            await self._do_buy(price, "buy")
        elif self.position > 0 and price >= self.sell_px:
            await self._do_sell(price, "sell")

    async def on_ticker(self, price, raw):
        if not self.running:
            return
        if self.params.get("use_signal"):
            await self._on_ticker_signal(price)
        else:
            await self._on_ticker_threshold(price)

    async def start(self):
        self.running = True
        logger.info(f"{self.inst_id} 低吸高卖已启动，模式={'信号' if self.params.get('use_signal') else '阈值'}")

    async def stop(self):
        self.running = False

    def snapshot(self):
        s = super().snapshot()
        s.update({
            "base_px": self.base_px,
            "buy_px": self.buy_px,
            "sell_px": self.sell_px,
            "position": self.position,
            "last_action": self._last_action,
            "total_profit": self.total_profit,
            "total_fee": self.total_fee,
            "trade_count": self.trade_count,
        })
        return s